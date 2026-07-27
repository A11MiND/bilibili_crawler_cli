"""Agent-friendly command-line interface for the Bilibili comment crawler."""

from __future__ import annotations

import argparse
import csv
import getpass
import inspect
import io
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence, TextIO

from . import __version__
from .api import (
    AccessDeniedError,
    AuthenticationRequiredError,
    BilibiliAPIError,
    BilibiliClient,
    BilibiliError,
    InvalidVideoInput,
    ResponseFormatError,
    RiskControlError,
    TemporaryNetworkError,
    VideoUnavailableError,
    extract_bvid,
)
from .crawler import CrawlResult, Crawler, CrawlStateError
from .storage import CheckpointStore, StorageError


PROGRAM_NAME = "bilibili-crawler"
DEFAULT_COOKIE_DIRECTORY = ".config/bili-comments"
DEFAULT_COOKIE_FILENAME = "cookie.txt"
JSON_SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ACCESS = 3
EXIT_UPSTREAM = 4
EXIT_LOCAL = 5
EXIT_AUTH = 6
EXIT_NOT_FOUND = 7
EXIT_INTERNAL = 70
EXIT_INTERRUPTED = 130

EXIT_CODES: dict[int, str] = {
    EXIT_OK: "success",
    EXIT_USAGE: "invalid input or configuration",
    EXIT_ACCESS: "video unavailable or access denied",
    EXIT_UPSTREAM: "temporary network, risk-control, or API failure",
    EXIT_LOCAL: "local state, storage, or response-format failure",
    EXIT_AUTH: "authentication required or invalid",
    EXIT_NOT_FOUND: "local crawl task not found",
    EXIT_INTERNAL: "unexpected internal failure in JSON mode",
    EXIT_INTERRUPTED: "interrupted",
}


class CookieConfigurationError(ValueError):
    """Raised when the selected cookie source cannot be used."""


class CliUsageError(ValueError):
    """Argument parser failure that can be represented as JSON."""

    def __init__(self, message: str, usage: str) -> None:
        self.usage = usage.strip()
        super().__init__(message)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message, self.format_usage())


def default_cookie_path() -> Path:
    """Return the per-user cookie path without resolving or printing it."""

    return Path.home() / DEFAULT_COOKIE_DIRECTORY / DEFAULT_COOKIE_FILENAME


def build_root_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog=PROGRAM_NAME,
        description="抓取 Bilibili 视频评论；支持稳定 JSON 输出和断点续爬。",
        epilog=(
            "命令: crawl, status, capabilities, auth set, auth check, auth path。\n"
            "兼容: bilibili-crawler <video> 与 python -m bili_comments <video>。\n"
            "--json 可放在命令前后任意位置。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="输出单个 JSON envelope")
    return parser


def build_crawl_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog=f"{PROGRAM_NAME} crawl",
        description="抓取单个 Bilibili 视频的一级及二级评论到 CSV。",
    )
    parser.add_argument(
        "video",
        nargs="?",
        help="BVID 或 Bilibili 视频网址；人类模式省略时交互输入",
    )
    authentication = parser.add_mutually_exclusive_group()
    authentication.add_argument(
        "--anonymous",
        action="store_true",
        help="匿名抓取；通常无法取得 IP 属地",
    )
    authentication.add_argument(
        "--cookie-file",
        type=Path,
        help="读取原始 Cookie 请求头的文件",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="备份已有 CSV/断点并从头抓取",
    )
    return parser


def build_auth_parser() -> argparse.ArgumentParser:
    """Build the legacy/auth-set parser retained for API compatibility."""

    parser = _ArgumentParser(
        prog=f"{PROGRAM_NAME} auth set",
        description="以隐藏输入保存 Bilibili Cookie（文件权限 600）。",
    )
    parser.add_argument(
        "--cookie-file",
        type=Path,
        default=None,
        help="保存位置；默认 ~/.config/bili-comments/cookie.txt",
    )
    parser.add_argument(
        "--from-env",
        action="store_true",
        help="从 BILI_COOKIE 读取；适用于非交互调用",
    )
    return parser


def build_auth_overview_parser() -> argparse.ArgumentParser:
    return _ArgumentParser(
        prog=f"{PROGRAM_NAME} auth",
        description="管理和验证 Bilibili 登录 Cookie。",
        epilog=(
            "子命令:\n"
            "  set    安全保存 Cookie（省略子命令仍兼容此行为）\n"
            "  check  联网验证 Cookie 是否仍为登录态\n"
            "  path   显示默认 Cookie 路径"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def build_auth_check_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog=f"{PROGRAM_NAME} auth check",
        description="联网验证已配置 Cookie 的登录状态。",
    )
    parser.add_argument("--cookie-file", type=Path, default=None)
    return parser


def build_auth_path_parser() -> argparse.ArgumentParser:
    return _ArgumentParser(
        prog=f"{PROGRAM_NAME} auth path",
        description="显示默认 Cookie 路径。",
    )


def build_status_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog=f"{PROGRAM_NAME} status",
        description="只读检查本地 checkpoint 与 CSV，不发送网络请求。",
    )
    parser.add_argument("video", help="BVID 或 Bilibili 视频网址")
    return parser


def build_capabilities_parser() -> argparse.ArgumentParser:
    return _ArgumentParser(
        prog=f"{PROGRAM_NAME} capabilities",
        description="输出命令、退出码及 JSON 契约。",
    )


def load_cookie(
    *,
    anonymous: bool,
    cookie_file: Path | None,
    environ: Mapping[str, str] | None = None,
    fallback_path: Path | None = None,
    enforce_file_security: bool = False,
) -> tuple[str | None, str]:
    """Load a cookie according to CLI, environment, then user config.

    ``enforce_file_security`` is enabled by every real CLI path.  It defaults
    to false only to preserve compatibility for callers that use this helper
    with synthetic files.
    """

    if anonymous:
        return None, "anonymous"

    if cookie_file is not None:
        return (
            _read_cookie_file(
                cookie_file,
                enforce_permissions=enforce_file_security,
            ),
            "file",
        )

    environment = os.environ if environ is None else environ
    environment_cookie = environment.get("BILI_COOKIE", "").strip()
    if environment_cookie:
        return (
            _validate_cookie_value(
                environment_cookie,
                "环境变量 BILI_COOKIE",
            ),
            "environment",
        )

    path = default_cookie_path() if fallback_path is None else fallback_path
    if path.is_file():
        return (
            _read_cookie_file(
                path,
                enforce_permissions=enforce_file_security,
            ),
            "default",
        )

    raise CookieConfigurationError(
        "未找到登录 Cookie。请先运行 `bilibili-crawler auth set`，"
        "（兼容命令：`python -m bili_comments auth`），"
        "设置 BILI_COOKIE，或显式使用 --anonymous"
    )


def save_cookie(cookie: str, path: Path) -> Path:
    """Atomically save a cookie with owner-only permissions."""

    value = cookie.strip()
    if not value:
        raise CookieConfigurationError("Cookie 不能为空")
    if "\n" in value or "\r" in value:
        raise CookieConfigurationError("Cookie 必须是单行请求头文本")

    destination = path.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        destination.chmod(0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return destination


def run_auth(
    argv: Sequence[str],
    *,
    getpass_fn: Callable[[str], str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    json_mode: bool = False,
) -> int:
    """Run ``auth set``; retained as the legacy ``auth`` implementation."""

    parser = build_auth_parser()
    args = parser.parse_args(list(argv))
    prompt = getpass.getpass if getpass_fn is None else getpass_fn
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    environment = os.environ if environ is None else environ
    destination = (
        default_cookie_path() if args.cookie_file is None else args.cookie_file
    )

    try:
        if args.from_env:
            cookie = _validate_cookie_value(
                environment.get("BILI_COOKIE", "").strip(),
                "环境变量 BILI_COOKIE",
            )
        elif json_mode:
            return _fail(
                "auth.set",
                EXIT_USAGE,
                "input_required",
                "JSON 模式不会等待输入；请设置 BILI_COOKIE 并使用 --from-env",
                out,
                err,
                json_mode=True,
            )
        else:
            cookie = prompt("请粘贴 Bilibili Cookie（输入不会显示）: ")
        saved_path = save_cookie(cookie, destination)
    except (EOFError, KeyboardInterrupt):
        return _fail(
            "auth.set",
            EXIT_INTERRUPTED,
            "interrupted",
            "已取消 Cookie 保存。",
            out,
            err,
            json_mode=json_mode,
        )
    except CookieConfigurationError as exc:
        return _fail(
            "auth.set",
            EXIT_USAGE,
            "cookie_configuration",
            str(exc),
            out,
            err,
            json_mode=json_mode,
            human_prefix="认证配置失败",
        )
    except OSError as exc:
        return _fail(
            "auth.set",
            EXIT_LOCAL,
            "local_io",
            str(exc),
            out,
            err,
            json_mode=json_mode,
            human_prefix="认证配置失败",
        )

    data = {
        "path": str(saved_path.expanduser().resolve()),
        "mode": "0600",
        "source": "environment" if args.from_env else "hidden_prompt",
    }
    if json_mode:
        _emit_envelope(out, "auth.set", EXIT_OK, data=data)
    else:
        print(f"Cookie 已安全保存到 {saved_path}（权限 600）", file=out)
    return EXIT_OK


def run_auth_check(
    argv: Sequence[str],
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[..., BilibiliClient] | None = None,
    json_mode: bool = False,
) -> int:
    parser = build_auth_check_parser()
    args = parser.parse_args(list(argv))
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    try:
        cookie, source = load_cookie(
            anonymous=False,
            cookie_file=args.cookie_file,
            environ=environ,
            enforce_file_security=True,
        )
        make_client = BilibiliClient if client_factory is None else client_factory
        client = make_client(cookie=cookie)
        if not client.validate_authentication():
            raise AuthenticationRequiredError(
                -101,
                "Cookie 未通过登录验证",
            )
    except CookieConfigurationError as exc:
        return _fail(
            "auth.check",
            EXIT_USAGE,
            "cookie_configuration",
            str(exc),
            out,
            err,
            json_mode=json_mode,
        )
    except AuthenticationRequiredError as exc:
        return _fail(
            "auth.check",
            EXIT_AUTH,
            "authentication_required",
            f"{exc}；请重新运行 `bilibili-crawler auth set`",
            out,
            err,
            json_mode=json_mode,
        )
    except (TemporaryNetworkError, RiskControlError, BilibiliAPIError) as exc:
        return _fail(
            "auth.check",
            EXIT_UPSTREAM,
            "upstream_error",
            str(exc),
            out,
            err,
            json_mode=json_mode,
        )
    except (OSError, ResponseFormatError) as exc:
        return _fail(
            "auth.check",
            EXIT_LOCAL,
            "local_or_response_error",
            str(exc),
            out,
            err,
            json_mode=json_mode,
        )

    data = {"authenticated": True, "cookie_source": source}
    if json_mode:
        _emit_envelope(out, "auth.check", EXIT_OK, data=data)
    else:
        print(f"Cookie 有效，当前为登录态（来源：{source}）。", file=out)
    return EXIT_OK


def run_auth_path(
    argv: Sequence[str],
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    json_mode: bool = False,
) -> int:
    parser = build_auth_path_parser()
    parser.parse_args(list(argv))
    out = sys.stdout if stdout is None else stdout
    path = default_cookie_path().resolve()
    data = {"path": str(path)}
    if json_mode:
        _emit_envelope(out, "auth.path", EXIT_OK, data=data)
    else:
        print(path, file=out)
    return EXIT_OK


def run_crawl(
    argv: Sequence[str],
    *,
    input_fn: Callable[[str], str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[..., BilibiliClient] | None = None,
    crawler_factory: Callable[..., Crawler] | None = None,
    json_mode: bool = False,
) -> int:
    parser = build_crawl_parser()
    args = parser.parse_args(list(argv))
    ask = input if input_fn is None else input_fn
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    video_value = args.video
    if video_value is None:
        if json_mode:
            return _fail(
                "crawl",
                EXIT_USAGE,
                "video_required",
                "JSON 模式不会等待输入；请提供视频 URL 或 BV 号",
                out,
                err,
                json_mode=True,
            )
        try:
            video_value = ask("请输入 Bilibili 视频网址或 BV 号: ").strip()
        except (EOFError, KeyboardInterrupt):
            return _fail(
                "crawl",
                EXIT_INTERRUPTED,
                "interrupted",
                "未读取到视频地址。",
                out,
                err,
                json_mode=False,
            )
    if not video_value or not video_value.strip():
        return _fail(
            "crawl",
            EXIT_USAGE,
            "invalid_video",
            "视频地址或 BV 号不能为空。",
            out,
            err,
            json_mode=json_mode,
        )
    try:
        extract_bvid(video_value.strip())
    except InvalidVideoInput as exc:
        return _fail(
            "crawl",
            EXIT_USAGE,
            "invalid_video",
            str(exc),
            out,
            err,
            json_mode=json_mode,
            human_prefix="输入无效",
        )

    try:
        cookie, cookie_source = load_cookie(
            anonymous=args.anonymous,
            cookie_file=args.cookie_file,
            environ=environ,
            enforce_file_security=True,
        )
    except CookieConfigurationError as exc:
        return _fail(
            "crawl",
            EXIT_USAGE,
            "cookie_configuration",
            str(exc),
            out,
            err,
            json_mode=json_mode,
        )

    auth_mode = (
        "anonymous" if cookie_source == "anonymous" else "authenticated"
    )
    if auth_mode == "anonymous":
        _progress("匿名模式：IP 属地通常为空。", out, err, json_mode)
    else:
        _progress(
            "已安全加载 Cookie，正在验证登录状态…",
            out,
            err,
            json_mode,
        )

    make_client = BilibiliClient if client_factory is None else client_factory
    make_crawler = Crawler if crawler_factory is None else crawler_factory
    crawler_started = False

    try:
        client = make_client(cookie=cookie)
        if auth_mode == "authenticated" and not client.validate_authentication():
            raise AuthenticationRequiredError(
                -101,
                "Cookie 未通过登录验证",
            )
        _progress("正在解析视频信息…", out, err, json_mode)
        video = client.resolve_video(video_value.strip())
        reply_total = (
            "未知" if video.reply_count is None else str(video.reply_count)
        )
        _progress(
            f"视频：{video.title}（{video.bvid}，页面评论数 {reply_total}）",
            out,
            err,
            json_mode,
        )
        crawler = make_crawler(
            client,
            progress=lambda message: _progress(
                message,
                out,
                err,
                json_mode,
            ),
        )
        crawler_started = True
        result = _run_crawler(
            crawler,
            video,
            restart=args.restart,
            auth_mode=auth_mode,
        )
    except KeyboardInterrupt:
        message = (
            "已中断；已成功提交的 CSV 和断点均已保留，"
            "重新运行同一命令即可续爬。"
            if crawler_started
            else "已中断；抓取尚未开始，尚未创建断点。"
        )
        return _fail(
            "crawl",
            EXIT_INTERRUPTED,
            "interrupted",
            message,
            out,
            err,
            json_mode=json_mode,
        )
    except AuthenticationRequiredError as exc:
        return _fail(
            "crawl",
            EXIT_AUTH,
            "authentication_required",
            f"{exc}；请重新运行 `bilibili-crawler auth set`",
            out,
            err,
            json_mode=json_mode,
            human_prefix="登录认证失败",
        )
    except (AccessDeniedError, VideoUnavailableError) as exc:
        return _fail(
            "crawl",
            EXIT_ACCESS,
            "video_unavailable",
            str(exc),
            out,
            err,
            json_mode=json_mode,
            human_prefix="视频不可访问",
        )
    except (TemporaryNetworkError, RiskControlError) as exc:
        message = str(exc)
        if crawler_started:
            message += "；断点可能已保留，稍后运行同一命令即可续爬"
        return _fail(
            "crawl",
            EXIT_UPSTREAM,
            "temporary_upstream_error",
            message,
            out,
            err,
            json_mode=json_mode,
            human_prefix="抓取暂时中断",
        )
    except (
        CrawlStateError,
        ResponseFormatError,
        StorageError,
        OSError,
    ) as exc:
        return _fail(
            "crawl",
            EXIT_LOCAL,
            "local_or_response_error",
            str(exc),
            out,
            err,
            json_mode=json_mode,
            human_prefix="本地状态或数据错误",
        )
    except (BilibiliAPIError, BilibiliError) as exc:
        return _fail(
            "crawl",
            EXIT_UPSTREAM,
            "upstream_api_error",
            str(exc),
            out,
            err,
            json_mode=json_mode,
            human_prefix="Bilibili 接口错误",
        )

    data = _crawl_result_data(result, auth_mode=auth_mode)
    if json_mode:
        _emit_envelope(out, "crawl", EXIT_OK, data=data)
    else:
        _print_summary(result, out)
    return EXIT_OK


def run_status(
    argv: Sequence[str],
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    json_mode: bool = False,
) -> int:
    parser = build_status_parser()
    args = parser.parse_args(list(argv))
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    try:
        bvid = extract_bvid(args.video)
    except InvalidVideoInput as exc:
        return _fail(
            "status",
            EXIT_USAGE,
            "invalid_video",
            str(exc),
            out,
            err,
            json_mode=json_mode,
        )

    csv_path = Path("output") / f"{bvid}.csv"
    state_path = Path("state") / f"{bvid}.json"
    csv_exists = csv_path.is_file()
    state_exists = state_path.is_file()
    base_data: dict[str, Any] = {
        "bvid": bvid,
        "csv_path": str(csv_path.resolve()),
        "checkpoint_path": str(state_path.resolve()),
        "csv_exists": csv_exists,
        "checkpoint_exists": state_exists,
    }
    if not csv_exists and not state_exists:
        return _fail(
            "status",
            EXIT_NOT_FOUND,
            "not_found",
            f"未找到 {bvid} 的本地抓取任务",
            out,
            err,
            json_mode=json_mode,
            data=base_data,
        )
    if csv_exists != state_exists:
        base_data["consistent"] = False
        return _fail(
            "status",
            EXIT_LOCAL,
            "incomplete_local_state",
            "CSV 与 checkpoint 不成对",
            out,
            err,
            json_mode=json_mode,
            data=base_data,
        )

    try:
        checkpoint = CheckpointStore("state", bvid).load()
        if checkpoint is None:
            raise StorageError("checkpoint 不存在")
        csv_data = _read_committed_csv_status(
            csv_path,
            checkpoint.committed_bytes,
        )
        if csv_data["row_count"] != checkpoint.rows_written:
            raise StorageError(
                "checkpoint rows_written 与已提交 CSV 行数不一致"
            )
    except (StorageError, OSError, UnicodeError, csv.Error) as exc:
        return _fail(
            "status",
            EXIT_LOCAL,
            "invalid_local_state",
            str(exc),
            out,
            err,
            json_mode=json_mode,
            data=base_data,
        )

    data = {
        **base_data,
        "consistent": True,
        "status": checkpoint.status,
        "phase": checkpoint.phase,
        "updated_at": checkpoint.updated_at,
        "rows_written": checkpoint.rows_written,
        "committed_bytes": checkpoint.committed_bytes,
        "auth_mode": getattr(checkpoint, "auth_mode", None),
        **csv_data,
    }
    if json_mode:
        _emit_envelope(out, "status", EXIT_OK, data=data)
    else:
        print(f"{bvid}：{checkpoint.status}（阶段 {checkpoint.phase}）", file=out)
        print(
            f"已提交 {checkpoint.rows_written} 行；"
            f"IP 属地 {csv_data['ip_location_count']} 行",
            file=out,
        )
        print(f"CSV：{csv_path.resolve()}", file=out)
        print(f"断点：{state_path.resolve()}", file=out)
    return EXIT_OK


def run_capabilities(
    argv: Sequence[str],
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    json_mode: bool = False,
) -> int:
    parser = build_capabilities_parser()
    parser.parse_args(list(argv))
    out = sys.stdout if stdout is None else stdout
    data = _capabilities_data()
    if json_mode:
        _emit_envelope(out, "capabilities", EXIT_OK, data=data)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), file=out)
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    input_fn: Callable[[str], str] | None = None,
    getpass_fn: Callable[[str], str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[..., BilibiliClient] | None = None,
    crawler_factory: Callable[..., Crawler] | None = None,
) -> int:
    """Route human and machine-friendly commands."""

    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in raw_arguments
    arguments = [item for item in raw_arguments if item != "--json"]
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    command = _command_name(arguments)

    if any(item in {"-h", "--help"} for item in arguments):
        if json_mode:
            _emit_envelope(
                out,
                "help",
                EXIT_OK,
                data={
                    "message": "机器客户端请使用 capabilities 获取稳定契约",
                    "capabilities": _capabilities_data(),
                },
            )
            return EXIT_OK
        _help_parser(arguments).print_help(file=out)
        return EXIT_OK

    try:
        if arguments and arguments[0] == "crawl":
            return run_crawl(
                arguments[1:],
                input_fn=input_fn,
                stdout=out,
                stderr=err,
                environ=environ,
                client_factory=client_factory,
                crawler_factory=crawler_factory,
                json_mode=json_mode,
            )
        if arguments and arguments[0] == "status":
            return run_status(
                arguments[1:],
                stdout=out,
                stderr=err,
                json_mode=json_mode,
            )
        if arguments and arguments[0] == "capabilities":
            return run_capabilities(
                arguments[1:],
                stdout=out,
                stderr=err,
                json_mode=json_mode,
            )
        if arguments and arguments[0] == "auth":
            auth_arguments = arguments[1:]
            if auth_arguments and auth_arguments[0] == "check":
                return run_auth_check(
                    auth_arguments[1:],
                    stdout=out,
                    stderr=err,
                    environ=environ,
                    client_factory=client_factory,
                    json_mode=json_mode,
                )
            if auth_arguments and auth_arguments[0] == "path":
                return run_auth_path(
                    auth_arguments[1:],
                    stdout=out,
                    stderr=err,
                    json_mode=json_mode,
                )
            if auth_arguments and auth_arguments[0] == "set":
                auth_arguments = auth_arguments[1:]
            return run_auth(
                auth_arguments,
                getpass_fn=getpass_fn,
                stdout=out,
                stderr=err,
                environ=environ,
                json_mode=json_mode,
            )

        # Legacy contract: no explicit command means crawl.
        return run_crawl(
            arguments,
            input_fn=input_fn,
            stdout=out,
            stderr=err,
            environ=environ,
            client_factory=client_factory,
            crawler_factory=crawler_factory,
            json_mode=json_mode,
        )
    except CliUsageError as exc:
        if json_mode:
            _emit_envelope(
                out,
                command,
                EXIT_USAGE,
                data={"usage": exc.usage},
                error={"code": "invalid_arguments", "message": str(exc)},
            )
        else:
            print(exc.usage, file=err)
            print(f"参数错误：{exc}", file=err)
        return EXIT_USAGE
    except Exception as exc:
        if not json_mode:
            raise
        _emit_envelope(
            out,
            command,
            EXIT_INTERNAL,
            error={
                "code": "internal_error",
                "message": f"{type(exc).__name__}: {exc}",
            },
        )
        return EXIT_INTERNAL


def _read_cookie_file(
    path: Path,
    *,
    enforce_permissions: bool = True,
) -> str:
    expanded = path.expanduser()
    try:
        if enforce_permissions:
            _validate_cookie_file_security(expanded)
        value = expanded.read_text(encoding="utf-8").strip()
    except CookieConfigurationError:
        raise
    except OSError as exc:
        raise CookieConfigurationError(
            f"无法读取 Cookie 文件 {path}: {exc}"
        ) from exc
    return _validate_cookie_value(value, f"Cookie 文件 {path}")


def _validate_cookie_file_security(path: Path) -> None:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise CookieConfigurationError(
            f"无法检查 Cookie 文件 {path}: {exc}"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise CookieConfigurationError(f"Cookie 路径不是普通文件：{path}")
    if os.name != "posix":
        return
    if metadata.st_uid != os.getuid():
        raise CookieConfigurationError(f"Cookie 文件所有者不是当前用户：{path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if mode & 0o077:
        raise CookieConfigurationError(
            f"Cookie 文件权限过宽（{mode:04o}）：{path}；请执行 chmod 600"
        )


def _validate_cookie_value(value: str, source: str) -> str:
    if not value:
        raise CookieConfigurationError(f"{source}为空")
    if "\n" in value or "\r" in value:
        raise CookieConfigurationError(f"{source}必须只有一行")
    return value


def _run_crawler(
    crawler: Crawler,
    video: object,
    *,
    restart: bool,
    auth_mode: str,
) -> CrawlResult:
    """Pass auth mode when supported without masking internal TypeErrors."""

    try:
        parameters = inspect.signature(crawler.run).parameters.values()
    except (TypeError, ValueError):
        supports_auth_mode = True
    else:
        supports_auth_mode = any(
            parameter.name == "auth_mode"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    if supports_auth_mode:
        return crawler.run(
            video,
            restart=restart,
            auth_mode=auth_mode,
        )
    return crawler.run(video, restart=restart)


def _read_committed_csv_status(
    path: Path,
    committed_bytes: int,
) -> dict[str, int]:
    raw = path.read_bytes()
    if len(raw) < committed_bytes:
        raise StorageError("CSV 小于 checkpoint 的 committed_bytes")
    try:
        text = raw[:committed_bytes].decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StorageError("已提交 CSV 不是有效 UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    required = {"评论ID", "隶属关系", "IP属地"}
    if reader.fieldnames is None or not required.issubset(reader.fieldnames):
        raise StorageError("CSV 表头不符合当前格式")
    row_count = root_count = child_count = ip_count = 0
    for row in reader:
        row_count += 1
        if row["隶属关系"] == "一级评论":
            root_count += 1
        elif row["隶属关系"] == "二级评论":
            child_count += 1
        if (row.get("IP属地") or "").strip():
            ip_count += 1
    return {
        "row_count": row_count,
        "root_count": root_count,
        "child_count": child_count,
        "ip_location_count": ip_count,
        "file_bytes": len(raw),
        "uncommitted_bytes": len(raw) - committed_bytes,
    }


def _crawl_result_data(
    result: CrawlResult,
    *,
    auth_mode: str,
) -> dict[str, Any]:
    total = result.total_count
    ip_count = result.ip_location_count
    return {
        "bvid": result.bvid,
        "auth_mode": auth_mode,
        "already_complete": result.already_complete,
        "counts": {
            "root": result.root_count,
            "child": result.child_count,
            "total": total,
            "ip_location": ip_count,
            "ip_location_missing": (
                None if ip_count is None else total - ip_count
            ),
        },
        "paths": {
            "csv": str(result.csv_path.resolve()),
            "checkpoint": str(result.state_path.resolve()),
        },
    }


def _capabilities_data() -> dict[str, Any]:
    return {
        "program": PROGRAM_NAME,
        "version": __version__,
        "commands": {
            "crawl": {
                "usage": (
                    f"{PROGRAM_NAME} crawl <video> "
                    "[--anonymous | --cookie-file PATH] [--restart]"
                ),
                "legacy_usage": f"{PROGRAM_NAME} <video>",
                "network": True,
                "interactive_without_json": True,
            },
            "status": {
                "usage": f"{PROGRAM_NAME} status <video>",
                "network": False,
                "read_only": True,
            },
            "capabilities": {
                "usage": f"{PROGRAM_NAME} capabilities",
                "network": False,
            },
            "auth.set": {
                "usage": (
                    f"{PROGRAM_NAME} auth set "
                    "[--cookie-file PATH] [--from-env]"
                ),
                "interactive_without_json": True,
            },
            "auth.check": {
                "usage": (
                    f"{PROGRAM_NAME} auth check [--cookie-file PATH]"
                ),
                "network": True,
            },
            "auth.path": {
                "usage": f"{PROGRAM_NAME} auth path",
                "network": False,
            },
        },
        "exit_codes": {
            str(code): meaning for code, meaning in sorted(EXIT_CODES.items())
        },
        "json_contract": {
            "flag": "--json",
            "placement": "anywhere",
            "stdout": "exactly one JSON object followed by one newline",
            "stdin": "never read implicitly in JSON mode",
            "progress": "stderr only",
            "envelope_schema_version": JSON_SCHEMA_VERSION,
            "envelope_fields": [
                "schema_version",
                "command",
                "ok",
                "exit_code",
                "data",
                "error",
            ],
        },
    }


def _emit_envelope(
    out: TextIO,
    command: str,
    exit_code: int,
    *,
    data: Mapping[str, Any] | None = None,
    error: Mapping[str, Any] | None = None,
) -> None:
    payload = {
        "schema_version": JSON_SCHEMA_VERSION,
        "command": command,
        "ok": exit_code == EXIT_OK,
        "exit_code": exit_code,
        "data": dict(data or {}),
        "error": dict(error) if error is not None else None,
    }
    out.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    out.write("\n")
    out.flush()


def _fail(
    command: str,
    exit_code: int,
    error_code: str,
    message: str,
    out: TextIO,
    err: TextIO,
    *,
    json_mode: bool,
    human_prefix: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> int:
    if json_mode:
        _emit_envelope(
            out,
            command,
            exit_code,
            data=data,
            error={"code": error_code, "message": message},
        )
    else:
        text = f"{human_prefix}：{message}" if human_prefix else message
        print(text, file=err)
    return exit_code


def _progress(
    message: str,
    out: TextIO,
    err: TextIO,
    json_mode: bool,
) -> None:
    print(message, file=err if json_mode else out)


def _print_summary(result: CrawlResult, out: TextIO) -> None:
    if result.already_complete:
        print("该任务此前已经完成，未重复请求评论接口。", file=out)
    else:
        print("抓取完成。", file=out)
    print(
        f"一级评论：{result.root_count}；"
        f"二级评论：{result.child_count}；"
        f"总行数：{result.total_count}",
        file=out,
    )
    if result.ip_location_count is not None:
        missing = result.total_count - result.ip_location_count
        coverage = (
            0.0
            if result.total_count == 0
            else result.ip_location_count / result.total_count * 100
        )
        print(
            f"IP 属地：{result.ip_location_count}/{result.total_count}"
            f"（缺失 {missing}，覆盖率 {coverage:.2f}%）",
            file=out,
        )
    print(f"CSV：{result.csv_path.resolve()}", file=out)
    print(f"断点：{result.state_path.resolve()}", file=out)


def _command_name(arguments: Sequence[str]) -> str:
    if not arguments:
        return "crawl"
    if arguments[0] != "auth":
        return arguments[0] if arguments[0] in {
            "crawl",
            "status",
            "capabilities",
        } else "crawl"
    if len(arguments) > 1 and arguments[1] in {"set", "check", "path"}:
        return f"auth.{arguments[1]}"
    return "auth.set"


def _help_parser(arguments: Sequence[str]) -> argparse.ArgumentParser:
    if not arguments or arguments[0] in {"-h", "--help"}:
        return build_root_parser()
    if arguments[0] == "crawl":
        return build_crawl_parser()
    if arguments[0] == "status":
        return build_status_parser()
    if arguments[0] == "capabilities":
        return build_capabilities_parser()
    if arguments[0] == "auth":
        if len(arguments) > 1 and arguments[1] == "set":
            return build_auth_parser()
        if len(arguments) > 1 and arguments[1] == "check":
            return build_auth_check_parser()
        if len(arguments) > 1 and arguments[1] == "path":
            return build_auth_path_parser()
        return build_auth_overview_parser()
    return build_root_parser()

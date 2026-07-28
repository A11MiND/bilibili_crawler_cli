from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from bili_comments import __version__
from bili_comments.api import (
    AuthenticationRequiredError,
    TemporaryNetworkError,
)
from bili_comments.cli import _CrawlProgress, _display_width, main
from bili_comments.crawler import CrawlResult
from bili_comments.models import Author, VideoInfo
from bili_comments.storage import CheckpointStore, CsvRow, CsvStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BVID = "BV0000000000"
SYNTHETIC_AID = 10**30
SYNTHETIC_OWNER_ID = "synthetic-owner"
SYNTHETIC_COOKIE = "test_session=not-a-credential"
VIDEO = VideoInfo(
    aid=SYNTHETIC_AID,
    bvid=BVID,
    title="测试视频",
    owner=Author(mid=SYNTHETIC_OWNER_ID, name="测试 UP"),
    reply_count=2,
)


@contextmanager
def working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def parsed_single_envelope(value: str) -> dict[str, object]:
    lines = value.splitlines()
    if len(lines) != 1:
        raise AssertionError(f"expected one JSON line, got {len(lines)}: {value!r}")
    payload = json.loads(lines[0])
    if set(payload) != {
        "schema_version",
        "command",
        "ok",
        "exit_code",
        "data",
        "error",
    }:
        raise AssertionError(f"unexpected envelope keys: {payload!r}")
    return payload


class AgentJsonSubprocessTests(unittest.TestCase):
    def run_module(
        self,
        *arguments: str,
        cwd: Path | None = None,
        timeout: float = 5,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        current_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(PROJECT_ROOT)
            if not current_pythonpath
            else f"{PROJECT_ROOT}{os.pathsep}{current_pythonpath}"
        )
        return subprocess.run(
            [sys.executable, "-m", "bili_comments", *arguments],
            cwd=cwd or PROJECT_ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def test_capabilities_is_single_stable_envelope_in_both_flag_positions(
        self,
    ) -> None:
        for arguments in (
            ("--json", "capabilities"),
            ("capabilities", "--json"),
        ):
            with self.subTest(arguments=arguments):
                completed = self.run_module(*arguments)
                self.assertEqual(completed.returncode, 0)
                payload = parsed_single_envelope(completed.stdout)
                self.assertEqual(payload["command"], "capabilities")
                self.assertTrue(payload["ok"])
                data = payload["data"]
                assert isinstance(data, dict)
                self.assertEqual(data["program"], "bilibili-crawler")
                self.assertEqual(data["version"], __version__)
                self.assertIn("auth.check", data["commands"])
                self.assertEqual(data["exit_codes"]["6"], "authentication required or invalid")
                self.assertEqual(completed.stderr, "")

    def test_json_crawl_without_video_never_waits_for_input(self) -> None:
        completed = self.run_module("crawl", "--json", timeout=2)
        self.assertEqual(completed.returncode, 2)
        payload = parsed_single_envelope(completed.stdout)
        self.assertEqual(payload["error"]["code"], "video_required")
        self.assertEqual(completed.stderr, "")

    def test_status_not_found_is_machine_readable_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            completed = self.run_module(
                "status",
                BVID,
                "--json",
                cwd=root,
            )
            self.assertEqual(completed.returncode, 7)
            payload = parsed_single_envelope(completed.stdout)
            self.assertEqual(payload["command"], "status")
            self.assertEqual(payload["error"]["code"], "not_found")
            self.assertFalse((root / "output").exists())
            self.assertFalse((root / "state").exists())

    def test_json_help_is_an_envelope_not_free_form_help(self) -> None:
        completed = self.run_module("auth", "check", "--json", "--help")
        self.assertEqual(completed.returncode, 0)
        payload = parsed_single_envelope(completed.stdout)
        self.assertEqual(payload["command"], "help")
        self.assertIn("capabilities", payload["data"])


class AgentCrawlTests(unittest.TestCase):
    def test_authenticated_json_crawl_validates_login_and_passes_auth_mode(
        self,
    ) -> None:
        calls: list[object] = []

        class FakeClient:
            def __init__(self, cookie: str | None) -> None:
                calls.append(("cookie", cookie))

            def validate_authentication(self) -> bool:
                calls.append("validate")
                return True

            def resolve_video(self, value: str) -> VideoInfo:
                calls.append(("resolve", value))
                return VIDEO

        class FakeCrawler:
            def __init__(self, client: object, *, progress: object) -> None:
                calls.append("crawler")

            def run(
                self,
                video: VideoInfo,
                *,
                restart: bool = False,
                auth_mode: str,
            ) -> CrawlResult:
                calls.append(("run", restart, auth_mode))
                return CrawlResult(
                    bvid=video.bvid,
                    csv_path=Path("output") / f"{video.bvid}.csv",
                    state_path=Path("state") / f"{video.bvid}.json",
                    root_count=1,
                    child_count=1,
                    total_count=2,
                    ip_location_count=2,
                )

        out = io.StringIO()
        err = io.StringIO()
        exit_code = main(
            ["crawl", BVID, "--json"],
            stdout=out,
            stderr=err,
            environ={"BILI_COOKIE": SYNTHETIC_COOKIE},
            client_factory=FakeClient,
            crawler_factory=FakeCrawler,
        )

        self.assertEqual(exit_code, 0)
        payload = parsed_single_envelope(out.getvalue())
        self.assertEqual(payload["data"]["auth_mode"], "authenticated")
        self.assertEqual(payload["data"]["counts"]["ip_location"], 2)
        self.assertEqual(
            calls,
            [
                ("cookie", SYNTHETIC_COOKIE),
                "validate",
                ("resolve", BVID),
                "crawler",
                ("run", False, "authenticated"),
            ],
        )
        self.assertIn("正在验证登录状态", err.getvalue())
        self.assertNotIn("正在验证登录状态", out.getvalue())

    def test_old_crawler_test_double_without_auth_mode_still_works(self) -> None:
        class FakeClient:
            def __init__(self, cookie: str | None) -> None:
                pass

            def resolve_video(self, value: str) -> VideoInfo:
                return VIDEO

        class LegacyCrawler:
            def __init__(self, client: object, *, progress: object) -> None:
                pass

            def run(
                self,
                video: VideoInfo,
                *,
                restart: bool = False,
            ) -> CrawlResult:
                return CrawlResult(
                    video.bvid,
                    Path("x.csv"),
                    Path("x.json"),
                    0,
                    0,
                    0,
                    0,
                )

        out = io.StringIO()
        exit_code = main(
            ["--anonymous", BVID, "--json"],
            stdout=out,
            stderr=io.StringIO(),
            client_factory=FakeClient,
            crawler_factory=LegacyCrawler,
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(parsed_single_envelope(out.getvalue())["ok"])

    def test_authentication_failure_has_dedicated_exit_code(self) -> None:
        class InvalidClient:
            def __init__(self, cookie: str | None) -> None:
                pass

            def validate_authentication(self) -> bool:
                raise AuthenticationRequiredError(-101, "账号未登录")

        out = io.StringIO()
        exit_code = main(
            [BVID, "--json"],
            stdout=out,
            stderr=io.StringIO(),
            environ={
                "BILI_COOKIE": "test_session=expired-placeholder"
            },
            client_factory=InvalidClient,
        )
        self.assertEqual(exit_code, 6)
        payload = parsed_single_envelope(out.getvalue())
        self.assertEqual(payload["error"]["code"], "authentication_required")

    def test_pre_crawl_network_failure_does_not_claim_checkpoint_exists(
        self,
    ) -> None:
        class FailingClient:
            def __init__(self, cookie: str | None) -> None:
                pass

            def resolve_video(self, value: str) -> VideoInfo:
                raise TemporaryNetworkError("timeout", attempts=1)

        out = io.StringIO()
        err = io.StringIO()
        exit_code = main(
            ["--anonymous", BVID],
            stdout=out,
            stderr=err,
            client_factory=FailingClient,
        )
        self.assertEqual(exit_code, 4)
        self.assertNotIn("断点", err.getvalue())


class ProgressRenderingTests(unittest.TestCase):
    class TtyBuffer(io.StringIO):
        def isatty(self) -> bool:
            return True

    def test_tty_progress_renders_counts_percentage_rate_and_cleans_line(
        self,
    ) -> None:
        ticks = iter([0.0, 2.0, 4.0, 6.0, 8.0])
        err = self.TtyBuffer()
        progress = _CrawlProgress(
            stdout=io.StringIO(),
            stderr=err,
            json_mode=False,
            expected_total=100,
            clock=lambda: next(ticks),
        )

        with mock.patch(
            "bili_comments.cli._terminal_columns",
            return_value=240,
        ):
            progress.update(
                f"开始抓取 {BVID}：已保存 0 条，从断点继续"
            )
            progress.update("一级评论 #1 100（已写入）")
            progress.update("二级评论第 1 页：新增 20 条，累计 21 条")
            progress.finish(
                CrawlResult(
                    BVID,
                    Path("output.csv"),
                    Path("state.json"),
                    1,
                    20,
                    21,
                    21,
                )
            )

        rendered = err.getvalue()
        self.assertIn("\r\x1b[2K", rendered)
        self.assertIn("≈21.0%", rendered)
        self.assertIn("已写入 21", rendered)
        self.assertIn("一级 1", rendered)
        self.assertIn("二级 20", rendered)
        self.assertIn("条/s", rendered)
        self.assertIn("耗时 00:08", rendered)
        self.assertTrue(rendered.endswith("\r\x1b[2K"))

    def test_non_tty_and_json_progress_never_emit_ansi(self) -> None:
        human_out = io.StringIO()
        human_err = io.StringIO()
        human = _CrawlProgress(
            stdout=human_out,
            stderr=human_err,
            json_mode=False,
            expected_total=None,
        )
        human.update("请求一级评论页（首页）")
        human.close()
        self.assertEqual(human_out.getvalue(), "请求一级评论页（首页）\n")
        self.assertEqual(human_err.getvalue(), "")
        self.assertNotIn("\x1b", human_out.getvalue())

        json_out = io.StringIO()
        json_err = self.TtyBuffer()
        machine = _CrawlProgress(
            stdout=json_out,
            stderr=json_err,
            json_mode=True,
            expected_total=10,
        )
        machine.update("一级评论 #1 100（已写入）")
        machine.close()
        self.assertEqual(json_out.getvalue(), "")
        self.assertEqual(json_err.getvalue(), "一级评论 #1 100（已写入）\n")
        self.assertNotIn("\x1b", json_err.getvalue())

    def test_detail_cursor_progress_updates_counts(self) -> None:
        progress = _CrawlProgress(
            stdout=io.StringIO(),
            stderr=self.TtyBuffer(),
            json_mode=False,
            expected_total=100,
            clock=lambda: 0.0,
        )

        progress.update(
            f"开始抓取 {BVID}：已保存 0 条，从断点继续"
        )
        progress.update("一级评论 #1 100（已写入）")
        progress.update("二级评论明细 next=0：新增 20 条，累计 21 条")

        self.assertEqual(progress.written, 21)
        self.assertEqual(progress.root_count, 1)
        self.assertEqual(progress.child_count, 20)
        progress.close()

    def test_tty_progress_truncates_by_terminal_display_width(self) -> None:
        err = self.TtyBuffer()
        progress = _CrawlProgress(
            stdout=io.StringIO(),
            stderr=err,
            json_mode=False,
            expected_total=100,
            clock=lambda: 0.0,
        )

        with mock.patch(
            "bili_comments.cli._terminal_columns",
            return_value=50,
        ):
            progress.update("一级评论 #1 100（已写入）")

        rendered_line = err.getvalue().rsplit("\r\x1b[2K", 1)[-1]
        self.assertLessEqual(_display_width(rendered_line), 49)
        self.assertTrue(rendered_line.endswith("…"))
        self.assertEqual(_display_width("A中e\u0301"), 4)
        progress.close()

    def test_tty_line_is_cleared_before_crawl_error_message(self) -> None:
        class FakeClient:
            def __init__(self, cookie: str | None) -> None:
                pass

            def resolve_video(self, value: str) -> VideoInfo:
                return VIDEO

        class FailingCrawler:
            def __init__(self, client: object, *, progress: object) -> None:
                self.progress = progress

            def run(
                self,
                video: VideoInfo,
                *,
                restart: bool = False,
                auth_mode: str,
            ) -> CrawlResult:
                self.progress(
                    f"开始抓取 {video.bvid}：已保存 0 条，从断点继续"
                )
                self.progress("一级评论 #1 100（已写入）")
                raise TemporaryNetworkError("timeout", attempts=1)

        with tempfile.TemporaryDirectory() as temporary:
            with working_directory(Path(temporary)):
                out = io.StringIO()
                err = self.TtyBuffer()
                exit_code = main(
                    ["crawl", "--anonymous", BVID],
                    stdout=out,
                    stderr=err,
                    client_factory=FakeClient,
                    crawler_factory=FailingCrawler,
                )

        self.assertEqual(exit_code, 4)
        rendered = err.getvalue()
        error_position = rendered.index("抓取暂时中断")
        self.assertGreater(
            rendered.rfind("\r\x1b[2K", 0, error_position),
            -1,
        )
        self.assertNotIn("\x1b", rendered[error_position:])


class CredentialAndStatusTests(unittest.TestCase):
    def create_complete_status_task(self) -> tuple[Path, Path]:
        csv_path = Path("output") / f"{BVID}.csv"
        csv_store = CsvStore(csv_path)
        csv_store.append_rows(
            [
                CsvRow(
                    root_sequence=1,
                    relation="一级评论",
                    comment_id="100",
                    root_id="100",
                    parent_id=None,
                    replied_to_name="测试 UP",
                    replied_to_id=SYNTHETIC_OWNER_ID,
                    author_name="测试作者",
                    author_id="synthetic-author",
                    content="SAFE",
                    published_at="2026-07-28 12:00:00+08:00",
                    like_count=1,
                    ip_location="IP属地：测试地区",
                )
            ]
        )
        checkpoint_store = CheckpointStore("state", BVID)
        checkpoint = checkpoint_store.create(
            aid=SYNTHETIC_AID,
            committed_bytes=csv_store.committed_bytes,
        )
        checkpoint.status = "complete"
        checkpoint.phase = "complete"
        checkpoint.completed_root_ids_in_page = ["100"]
        checkpoint.next_root_sequence = 2
        checkpoint.rows_written = 1
        checkpoint_store.save(checkpoint)
        return csv_path, checkpoint_store.path

    def assert_status_rejects_without_mutation(
        self,
        csv_path: Path,
        state_path: Path,
    ) -> dict[str, object]:
        csv_before = csv_path.read_bytes()
        state_before = state_path.read_bytes()
        out = io.StringIO()

        exit_code = main(
            ["status", "--json", BVID],
            stdout=out,
            stderr=io.StringIO(),
        )

        self.assertEqual(exit_code, 5)
        payload = parsed_single_envelope(out.getvalue())
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["data"]["consistent"])
        self.assertEqual(payload["error"]["code"], "invalid_local_state")
        self.assertEqual(csv_path.read_bytes(), csv_before)
        self.assertEqual(state_path.read_bytes(), state_before)
        return payload

    @unittest.skipUnless(os.name == "posix", "POSIX permission contract")
    def test_cli_rejects_group_or_world_readable_cookie_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cookie_file = Path(temporary) / "cookie.txt"
            cookie_file.write_text(
                f"{SYNTHETIC_COOKIE}\n",
                encoding="utf-8",
            )
            cookie_file.chmod(0o644)
            out = io.StringIO()

            exit_code = main(
                [
                    "crawl",
                    BVID,
                    "--cookie-file",
                    str(cookie_file),
                    "--json",
                ],
                stdout=out,
                stderr=io.StringIO(),
                client_factory=lambda **_: self.fail(
                    "insecure cookie must be rejected before client creation"
                ),
            )

            self.assertEqual(exit_code, 2)
            payload = parsed_single_envelope(out.getvalue())
            self.assertEqual(payload["error"]["code"], "cookie_configuration")
            self.assertIn("chmod 600", payload["error"]["message"])

    @unittest.skipUnless(os.name == "posix", "POSIX no-follow contract")
    def test_cli_rejects_symlink_cookie_without_reading_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.txt"
            target.write_text(
                f"{SYNTHETIC_COOKIE}\n",
                encoding="utf-8",
            )
            target.chmod(0o600)
            original = target.read_bytes()
            cookie_file = root / "cookie.txt"
            cookie_file.symlink_to(target)
            out = io.StringIO()
            err = io.StringIO()

            exit_code = main(
                [
                    "crawl",
                    BVID,
                    "--cookie-file",
                    str(cookie_file),
                    "--json",
                ],
                stdout=out,
                stderr=err,
                client_factory=lambda **_: self.fail(
                    "symbolic-link cookie must be rejected before reading"
                ),
            )

            self.assertEqual(exit_code, 2)
            payload = parsed_single_envelope(out.getvalue())
            self.assertEqual(payload["error"]["code"], "cookie_configuration")
            self.assertNotIn(SYNTHETIC_COOKIE, out.getvalue())
            self.assertNotIn(SYNTHETIC_COOKIE, err.getvalue())
            self.assertEqual(target.read_bytes(), original)
            self.assertTrue(cookie_file.is_symlink())

    @unittest.skipUnless(os.name == "posix", "POSIX single-link contract")
    def test_cli_rejects_hard_link_cookie_without_reading_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target.txt"
            target.write_text(
                f"{SYNTHETIC_COOKIE}\n",
                encoding="utf-8",
            )
            target.chmod(0o600)
            original = target.read_bytes()
            cookie_file = root / "cookie.txt"
            os.link(target, cookie_file)
            out = io.StringIO()
            err = io.StringIO()

            exit_code = main(
                [
                    "crawl",
                    BVID,
                    "--cookie-file",
                    str(cookie_file),
                    "--json",
                ],
                stdout=out,
                stderr=err,
                client_factory=lambda **_: self.fail(
                    "multi-link cookie must be rejected before reading"
                ),
            )

            self.assertEqual(exit_code, 2)
            payload = parsed_single_envelope(out.getvalue())
            self.assertEqual(payload["error"]["code"], "cookie_configuration")
            self.assertIn("硬链接", payload["error"]["message"])
            self.assertNotIn(SYNTHETIC_COOKIE, out.getvalue())
            self.assertNotIn(SYNTHETIC_COOKIE, err.getvalue())
            self.assertEqual(target.read_bytes(), original)

    @unittest.skipUnless(os.name == "posix", "POSIX regular-file contract")
    def test_cli_rejects_fifo_cookie_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cookie_file = Path(temporary) / "cookie.pipe"
            os.mkfifo(cookie_file, mode=0o600)
            out = io.StringIO()

            exit_code = main(
                [
                    "crawl",
                    BVID,
                    "--cookie-file",
                    str(cookie_file),
                    "--json",
                ],
                stdout=out,
                stderr=io.StringIO(),
                client_factory=lambda **_: self.fail(
                    "non-regular cookie must be rejected before reading"
                ),
            )

            self.assertEqual(exit_code, 2)
            payload = parsed_single_envelope(out.getvalue())
            self.assertEqual(payload["error"]["code"], "cookie_configuration")
            self.assertIn("不是普通文件", payload["error"]["message"])

    def test_cli_fails_closed_without_no_follow_support(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cookie_file = Path(temporary) / "cookie.txt"
            cookie_file.write_text(
                f"{SYNTHETIC_COOKIE}\n",
                encoding="utf-8",
            )
            original = cookie_file.read_bytes()
            out = io.StringIO()

            with mock.patch.object(os, "O_NOFOLLOW", None):
                exit_code = main(
                    [
                        "crawl",
                        BVID,
                        "--cookie-file",
                        str(cookie_file),
                        "--json",
                    ],
                    stdout=out,
                    stderr=io.StringIO(),
                    client_factory=lambda **_: self.fail(
                        "cookie must not be read without O_NOFOLLOW"
                    ),
                )

            self.assertEqual(exit_code, 2)
            payload = parsed_single_envelope(out.getvalue())
            self.assertEqual(payload["error"]["code"], "cookie_configuration")
            self.assertIn("O_NOFOLLOW", payload["error"]["message"])
            self.assertNotIn(SYNTHETIC_COOKIE, out.getvalue())
            self.assertEqual(cookie_file.read_bytes(), original)

    def test_auth_check_and_auth_path_are_noninteractive(self) -> None:
        class ValidClient:
            def __init__(self, cookie: str | None) -> None:
                self.cookie = cookie

            def validate_authentication(self) -> bool:
                return True

        out = io.StringIO()
        exit_code = main(
            ["auth", "--json", "check"],
            stdout=out,
            stderr=io.StringIO(),
            environ={"BILI_COOKIE": SYNTHETIC_COOKIE},
            client_factory=ValidClient,
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(parsed_single_envelope(out.getvalue())["data"]["authenticated"])

        path_out = io.StringIO()
        self.assertEqual(
            main(["auth", "path", "--json"], stdout=path_out),
            0,
        )
        self.assertTrue(parsed_single_envelope(path_out.getvalue())["data"]["path"])

    def test_status_reads_complete_local_task_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with working_directory(root):
                csv_path = Path("output") / f"{BVID}.csv"
                csv_store = CsvStore(csv_path)
                checkpoint_store = CheckpointStore("state", BVID)
                checkpoint = checkpoint_store.create(
                    aid=SYNTHETIC_AID,
                    committed_bytes=csv_store.committed_bytes,
                )
                checkpoint.status = "complete"
                checkpoint.phase = "complete"
                checkpoint_store.save(checkpoint)
                csv_before = csv_path.read_bytes()
                state_before = checkpoint_store.path.read_bytes()

                out = io.StringIO()
                exit_code = main(
                    ["status", "--json", BVID],
                    stdout=out,
                    stderr=io.StringIO(),
                )

                self.assertEqual(exit_code, 0)
                payload = parsed_single_envelope(out.getvalue())
                self.assertEqual(payload["data"]["status"], "complete")
                self.assertEqual(payload["data"]["row_count"], 0)
                self.assertEqual(csv_path.read_bytes(), csv_before)
                self.assertEqual(checkpoint_store.path.read_bytes(), state_before)

    def test_status_rejects_duplicate_comment_id_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with working_directory(Path(temporary)):
                csv_path, state_path = self.create_complete_status_task()
                raw = csv_path.read_bytes()
                first_record_end = raw.index(b"\r\n") + 2
                csv_path.write_bytes(raw + raw[first_record_end:])

                payload = self.assert_status_rejects_without_mutation(
                    csv_path,
                    state_path,
                )

                self.assertIn("duplicate comment ID", payload["error"]["message"])

    def test_status_rejects_partial_record_and_wrong_header(self) -> None:
        for corruption in ("partial_record", "wrong_header"):
            with (
                self.subTest(corruption=corruption),
                tempfile.TemporaryDirectory() as temporary,
            ):
                with working_directory(Path(temporary)):
                    csv_path, state_path = self.create_complete_status_task()
                    raw = csv_path.read_bytes()
                    if corruption == "partial_record":
                        csv_path.write_bytes(raw + b'"unterminated')
                    else:
                        csv_path.write_bytes(
                            raw.replace(
                                "一级评论序号".encode(),
                                "错误表头".encode(),
                                1,
                            )
                        )

                    self.assert_status_rejects_without_mutation(
                        csv_path,
                        state_path,
                    )

    def test_status_rejects_unescaped_formula_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with working_directory(Path(temporary)):
                csv_path, state_path = self.create_complete_status_task()
                raw = csv_path.read_bytes()
                self.assertIn(b"SAFE", raw)
                csv_path.write_bytes(raw.replace(b"SAFE", b"=1+1", 1))

                payload = self.assert_status_rejects_without_mutation(
                    csv_path,
                    state_path,
                )

                self.assertIn("Excel formula", payload["error"]["message"])

    def test_status_does_not_recreate_csv_that_disappears_after_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with working_directory(Path(temporary)):
                csv_path, state_path = self.create_complete_status_task()
                state_before = state_path.read_bytes()
                csv_path.unlink()
                original_is_file = Path.is_file

                def simulated_probe(path: Path) -> bool:
                    if path == csv_path:
                        return True
                    return original_is_file(path)

                out = io.StringIO()
                with mock.patch.object(
                    Path,
                    "is_file",
                    autospec=True,
                    side_effect=simulated_probe,
                ):
                    exit_code = main(
                        ["status", "--json", BVID],
                        stdout=out,
                        stderr=io.StringIO(),
                    )

                self.assertEqual(exit_code, 5)
                payload = parsed_single_envelope(out.getvalue())
                self.assertEqual(
                    payload["error"]["code"],
                    "invalid_local_state",
                )
                self.assertFalse(csv_path.exists())
                self.assertEqual(state_path.read_bytes(), state_before)

    def test_pyproject_exposes_primary_console_script_and_matching_version(
        self,
    ) -> None:
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as source:
            project = tomllib.load(source)["project"]
        self.assertEqual(
            project["scripts"]["bilibili-crawler"],
            "bili_comments.cli:main",
        )
        self.assertEqual(project["version"], __version__)


if __name__ == "__main__":
    unittest.main()

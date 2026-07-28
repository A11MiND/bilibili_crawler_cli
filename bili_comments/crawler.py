"""Resumable orchestration for root and child comment pagination."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .api import BilibiliClient, ChildPaginationLimitError
from .models import Comment, VideoInfo
from .storage import (
    AUTH_MODES,
    Checkpoint,
    CheckpointStore,
    CsvRow,
    CsvStore,
    TaskLock,
    backup_for_restart,
)


ProgressCallback = Callable[[str], None]
_CHINA_TIMEZONE = timezone(timedelta(hours=8))


class CrawlStateError(RuntimeError):
    """Raised when local state is inconsistent or pagination cannot advance."""


@dataclass(frozen=True)
class CrawlResult:
    """Reader-facing summary of a crawl."""

    bvid: str
    csv_path: Path
    state_path: Path
    root_count: int
    child_count: int
    total_count: int
    ip_location_count: int | None
    already_complete: bool = False


class Crawler:
    """Coordinate API pages with durable CSV and checkpoint commits."""

    def __init__(
        self,
        client: BilibiliClient,
        *,
        output_dir: str | Path = "output",
        state_dir: str | Path = "state",
        progress: ProgressCallback | None = None,
    ) -> None:
        self.client = client
        self.output_dir = Path(output_dir)
        self.state_dir = Path(state_dir)
        self.progress = progress

    def run(
        self,
        video: VideoInfo,
        *,
        restart: bool = False,
        auth_mode: str | None = None,
    ) -> CrawlResult:
        """Crawl one video while holding its exclusive task lock.

        ``auth_mode`` is persisted because anonymous and authenticated API
        responses expose different fields.  Callers may provide it explicitly;
        otherwise it is inferred from ``client.cookie``.
        """

        resolved_auth_mode = self._resolve_auth_mode(auth_mode)
        lock_path = self.state_dir / f"{video.bvid}.lock"
        with TaskLock(lock_path):
            return self._run_locked(
                video,
                restart=restart,
                auth_mode=resolved_auth_mode,
            )

    def _run_locked(
        self,
        video: VideoInfo,
        *,
        restart: bool,
        auth_mode: str,
    ) -> CrawlResult:
        """Execute a crawl after the caller has acquired the BVID lock."""

        csv_path = self.output_dir / f"{video.bvid}.csv"
        state_path = self.state_dir / f"{video.bvid}.json"
        checkpoint_store = CheckpointStore(self.state_dir, video.bvid)

        if restart:
            backups = backup_for_restart(csv_path, state_path)
            if backups:
                self._report(
                    "已备份旧任务: " + ", ".join(str(path) for path in backups)
                )

        csv_exists = csv_path.exists()
        state_exists = state_path.exists()
        if csv_exists and not state_exists:
            orphan_store = CsvStore(csv_path)
            if not orphan_store.is_header_only:
                raise CrawlStateError(
                    "CSV 与断点文件不成对，无法安全续爬；"
                    "请使用 --restart 备份后重抓"
                )
            csv_store = orphan_store
            checkpoint = checkpoint_store.create(
                video.aid,
                csv_store.committed_bytes,
                auth_mode,
            )
            self._report("检测到只有标准表头的 CSV，已安全重建初始断点")
        elif state_exists and not csv_exists:
            raise CrawlStateError(
                "CSV 与断点文件不成对，无法安全续爬；请使用 --restart 备份后重抓"
            )
        elif not csv_exists:
            csv_store = CsvStore(csv_path)
            checkpoint = checkpoint_store.create(
                video.aid,
                csv_store.committed_bytes,
                auth_mode,
            )
        else:
            checkpoint = checkpoint_store.load()
            if checkpoint is None:
                raise CrawlStateError(
                    "断点文件缺失或不可读取；请使用 --restart 备份后重抓"
                )
            if checkpoint.aid != video.aid:
                raise CrawlStateError(
                    "断点中的视频标识与当前视频不一致；请使用 --restart"
                )
            legacy_checkpoint = checkpoint.schema_version == 1
            if not legacy_checkpoint and checkpoint.auth_mode != auth_mode:
                raise CrawlStateError(
                    "断点的登录模式与本次运行不一致；请使用原模式续爬，"
                    "或使用 --restart 重新抓取"
                )
            csv_store = CsvStore(
                csv_path,
                committed_bytes=checkpoint.committed_bytes,
                expected_rows=checkpoint.rows_written,
            )
            if checkpoint.rows_written != csv_store.rows_written:
                raise CrawlStateError(
                    "断点 rows_written 与 CSV 唯一评论数不一致；"
                    "请使用 --restart"
                )
            unknown_completed_roots = set(
                checkpoint.completed_root_ids_in_page
            ) - set(csv_store.root_sequences)
            if unknown_completed_roots:
                raise CrawlStateError(
                    "断点包含 CSV 中不存在的已完成根评论；请使用 --restart"
                )
            expected_next_sequence = csv_store.max_root_sequence + 1
            if checkpoint.next_root_sequence != expected_next_sequence:
                raise CrawlStateError(
                    "断点中的一级评论序号与 CSV 不一致，无法安全续爬；"
                    "请使用 --restart"
                )
            if checkpoint.schema_version < 3:
                self._migrate_checkpoint(
                    checkpoint,
                    auth_mode,
                    csv_store,
                )
                checkpoint_store.save(checkpoint)

        if checkpoint.status == "complete":
            return self._result(
                video,
                csv_store,
                csv_path,
                state_path,
                already_complete=True,
            )

        self._report(
            f"开始抓取 {video.bvid}：已保存 {len(csv_store.seen_ids)} 条，"
            f"从断点继续"
        )

        # A crash can leave a root row committed while its child pagination is
        # in progress. Resume that child stream without relying on the root
        # still occupying the same main-page position.
        if checkpoint.current_root_id:
            root_id = str(checkpoint.current_root_id)
            root_sequence = csv_store.root_sequences.get(root_id)
            root_author = csv_store.authors.get(root_id)
            if root_sequence is None or root_author is None:
                raise CrawlStateError(
                    f"断点指向根评论 {root_id}，但 CSV 中找不到对应已提交行"
                )
            self._crawl_children(
                video,
                root_id,
                root_sequence,
                root_author,
                checkpoint.sub_cursor,
                checkpoint,
                checkpoint_store,
                csv_store,
            )

        seen_main_cursors: set[str] = set()
        while True:
            cursor_key = _cursor_key(checkpoint.main_cursor)
            if cursor_key in seen_main_cursors:
                raise CrawlStateError(
                    "一级评论游标重复，已停止以避免无限请求；可稍后续爬"
                )
            seen_main_cursors.add(cursor_key)

            self._report(
                "请求一级评论页"
                + (
                    "（首页）"
                    if checkpoint.main_cursor is None
                    else f"（cursor={checkpoint.main_cursor!r}）"
                )
            )
            page = self.client.fetch_root_page(video, checkpoint.main_cursor)
            checkpoint.next_main_cursor = page.next_cursor
            checkpoint.phase = "root_page"
            self._save_checkpoint(checkpoint, checkpoint_store, csv_store)

            completed = {
                str(root_id)
                for root_id in checkpoint.completed_root_ids_in_page
            }
            for root in page.items:
                root_id = str(root.rpid)
                if root_id in completed:
                    continue

                root_sequence = csv_store.root_sequences.get(root_id)
                if root_sequence is None:
                    root_sequence = checkpoint.next_root_sequence

                root_is_new = root_id not in csv_store.seen_ids
                if root_is_new:
                    csv_store.append_rows(
                        [_root_row(root, video, root_sequence)]
                    )
                    checkpoint.next_root_sequence = max(
                        checkpoint.next_root_sequence, root_sequence + 1
                    )

                if root.rcount > 0:
                    checkpoint.current_root_id = root_id
                    checkpoint.sub_cursor = 0
                    checkpoint.child_strategy = "detail"
                    checkpoint.phase = "child_page"
                else:
                    _append_unique(
                        checkpoint.completed_root_ids_in_page, root_id
                    )
                    completed.add(root_id)
                    checkpoint.current_root_id = None
                    checkpoint.sub_cursor = None
                    checkpoint.child_strategy = "page"

                self._save_checkpoint(
                    checkpoint, checkpoint_store, csv_store
                )
                self._report(
                    f"一级评论 #{root_sequence} {root_id}"
                    + ("（已写入）" if root_is_new else "（已存在）")
                )

                if root.rcount > 0:
                    self._crawl_children(
                        video,
                        root_id,
                        root_sequence,
                        (str(root.author.mid), root.author.name),
                        checkpoint.sub_cursor,
                        checkpoint,
                        checkpoint_store,
                        csv_store,
                    )
                    completed.add(root_id)

            if not page.has_more:
                checkpoint.status = "complete"
                checkpoint.phase = "complete"
                checkpoint.next_main_cursor = None
                checkpoint.current_root_id = None
                checkpoint.sub_cursor = None
                checkpoint.child_strategy = "page"
                self._save_checkpoint(
                    checkpoint, checkpoint_store, csv_store
                )
                break

            if page.next_cursor is None:
                raise CrawlStateError(
                    "接口表示仍有一级评论，但没有返回下一游标；已保留断点"
                )
            if _cursor_key(page.next_cursor) == _cursor_key(
                checkpoint.main_cursor
            ):
                raise CrawlStateError(
                    "接口返回了相同的一级评论游标；已停止以避免无限请求"
                )

            checkpoint.main_cursor = page.next_cursor
            checkpoint.next_main_cursor = None
            checkpoint.completed_root_ids_in_page = []
            checkpoint.current_root_id = None
            checkpoint.sub_cursor = None
            checkpoint.child_strategy = "page"
            checkpoint.phase = "root_page"
            self._save_checkpoint(checkpoint, checkpoint_store, csv_store)

        return self._result(video, csv_store, csv_path, state_path)

    def _resolve_auth_mode(self, requested: str | None) -> str:
        if requested is None:
            requested = (
                "authenticated"
                if bool(getattr(self.client, "cookie", None))
                else "anonymous"
            )
        if requested not in AUTH_MODES:
            raise CrawlStateError(
                "auth_mode 必须是 'anonymous' 或 'authenticated'"
            )
        return requested

    @staticmethod
    def _migrate_checkpoint(
        checkpoint: Checkpoint,
        auth_mode: str,
        csv_store: CsvStore,
    ) -> None:
        """Upgrade a validated v1/v2 checkpoint to the detail-cursor schema.

        Version 1 used ``sub_cursor=None`` both for "start at page 1" and
        "the final child page is committed".  The latter is distinguishable
        because it also retained ``phase=child_page`` and ``current_root_id``;
        migrate that state directly to root completion. Any genuinely active
        legacy child stream restarts detail pagination at cursor zero; the CSV
        comment-ID index safely de-duplicates the replay.
        """

        if checkpoint.schema_version not in {1, 2}:
            return
        if checkpoint.schema_version == 1:
            if csv_store.rows_written == 0:
                migrated_auth_mode = auth_mode
            elif csv_store.ip_location_count > 0:
                migrated_auth_mode = "authenticated"
                if auth_mode != migrated_auth_mode:
                    raise CrawlStateError(
                        "v1 断点中的 CSV 含 IP 属地，可确定原任务为登录模式；"
                        "本次匿名模式不能混用，请使用登录模式续爬或 --restart"
                    )
            else:
                raise CrawlStateError(
                    "v1 断点已有评论但没有可可靠推断登录模式的 IP 属地；"
                    "为避免混合匿名/登录数据，请使用 --restart"
                )
        else:
            migrated_auth_mode = checkpoint.auth_mode
        if (
            checkpoint.schema_version == 1
            and checkpoint.status == "running"
            and checkpoint.phase == "child_page"
            and checkpoint.current_root_id is not None
            and checkpoint.sub_cursor is None
        ):
            if (
                checkpoint.current_root_id not in csv_store.root_sequences
                or checkpoint.current_root_id not in csv_store.authors
            ):
                raise CrawlStateError(
                    "v1 断点指向的已完成根评论不在 CSV 中；"
                    "请使用 --restart"
                )
            _append_unique(
                checkpoint.completed_root_ids_in_page,
                checkpoint.current_root_id,
            )
            checkpoint.current_root_id = None
            checkpoint.child_strategy = "page"
            checkpoint.phase = "root_page"
        elif (
            checkpoint.status == "running"
            and checkpoint.phase == "child_page"
            and checkpoint.current_root_id is not None
        ):
            checkpoint.child_strategy = "detail"
            checkpoint.sub_cursor = 0
        checkpoint.schema_version = 3
        checkpoint.auth_mode = migrated_auth_mode
        checkpoint.validate()

    def _crawl_children(
        self,
        video: VideoInfo,
        root_id: str,
        root_sequence: int,
        root_author: tuple[str, str],
        start_cursor: object | None,
        checkpoint: Checkpoint,
        checkpoint_store: CheckpointStore,
        csv_store: CsvStore,
    ) -> None:
        strategy = checkpoint.child_strategy
        cursor = _child_cursor(start_cursor, strategy)
        seen_cursors: set[int] = set()

        while True:
            if cursor in seen_cursors:
                raise CrawlStateError(
                    f"根评论 {root_id} 的二级评论游标重复，已停止以避免无限请求"
                )
            seen_cursors.add(cursor)

            if strategy == "page":
                self._report(
                    f"请求一级评论 #{root_sequence} 的二级评论第 {cursor} 页"
                )
                try:
                    page = self.client.fetch_child_page(
                        video,
                        root_id,
                        cursor,
                    )
                except ChildPaginationLimitError:
                    # The detail endpoint is floor-ordered rather than
                    # popularity-ordered, so page offsets cannot be converted.
                    # Persist cursor zero before issuing the first detail
                    # request. Existing CSV rows de-duplicate the full replay.
                    strategy = "detail"
                    cursor = 0
                    seen_cursors.clear()
                    checkpoint.child_strategy = strategy
                    checkpoint.sub_cursor = cursor
                    checkpoint.current_root_id = root_id
                    checkpoint.phase = "child_page"
                    self._save_checkpoint(
                        checkpoint,
                        checkpoint_store,
                        csv_store,
                    )
                    self._report(
                        f"一级评论 #{root_sequence} 的旧分页达到服务端上限；"
                        "已切换明细游标并从 next=0 安全重扫"
                    )
                    continue
                progress_label = f"第 {cursor} 页"
            else:
                self._report(
                    f"请求一级评论 #{root_sequence} 的二级评论明细 "
                    f"next={cursor}"
                )
                page = self.client.fetch_child_detail_page(
                    video,
                    root_id,
                    cursor,
                )
                progress_label = f"明细 next={cursor}"

            page_authors = {
                str(comment.rpid): (
                    str(comment.author.mid),
                    comment.author.name,
                )
                for comment in page.items
            }
            known_authors = dict(csv_store.authors)
            known_authors.update(page_authors)
            rows = [
                _child_row(
                    comment,
                    root_id,
                    root_sequence,
                    root_author,
                    known_authors,
                )
                for comment in page.items
            ]
            written = csv_store.append_rows(rows)

            if page.has_more:
                if page.next_cursor is None:
                    raise CrawlStateError(
                        f"根评论 {root_id} 仍有二级评论，但没有返回下一游标"
                    )
                next_cursor = _child_cursor(page.next_cursor, strategy)
                if next_cursor <= cursor:
                    raise CrawlStateError(
                        f"根评论 {root_id} 的二级评论游标没有前进"
                    )
                checkpoint.sub_cursor = next_cursor
                checkpoint.current_root_id = root_id
                checkpoint.child_strategy = strategy
                checkpoint.phase = "child_page"
            else:
                _append_unique(
                    checkpoint.completed_root_ids_in_page,
                    root_id,
                )
                checkpoint.current_root_id = None
                checkpoint.sub_cursor = None
                checkpoint.child_strategy = "page"
                checkpoint.phase = "root_page"

            self._save_checkpoint(checkpoint, checkpoint_store, csv_store)
            self._report(
                f"二级评论{progress_label}：新增 {written} 条，"
                f"累计 {len(csv_store.seen_ids)} 条"
            )

            if not page.has_more:
                return
            cursor = _child_cursor(page.next_cursor, strategy)

    @staticmethod
    def _save_checkpoint(
        checkpoint: Checkpoint,
        checkpoint_store: CheckpointStore,
        csv_store: CsvStore,
    ) -> None:
        checkpoint.rows_written = len(csv_store.seen_ids)
        checkpoint.committed_bytes = csv_store.committed_bytes
        checkpoint_store.save(checkpoint)

    def _result(
        self,
        video: VideoInfo,
        csv_store: CsvStore,
        csv_path: Path,
        state_path: Path,
        *,
        already_complete: bool = False,
    ) -> CrawlResult:
        total_count = len(csv_store.seen_ids)
        root_count = len(csv_store.root_sequences)
        return CrawlResult(
            bvid=video.bvid,
            csv_path=csv_path,
            state_path=state_path,
            root_count=root_count,
            child_count=max(0, total_count - root_count),
            total_count=total_count,
            ip_location_count=_count_ip_locations(csv_path),
            already_complete=already_complete,
        )

    def _report(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)


def _root_row(
    comment: Comment, video: VideoInfo, root_sequence: int
) -> CsvRow:
    return CsvRow(
        root_sequence=root_sequence,
        relation="一级评论",
        comment_id=str(comment.rpid),
        root_id=str(comment.rpid),
        parent_id=None,
        replied_to_name=video.owner.name,
        replied_to_id=str(video.owner.mid),
        author_name=comment.author.name,
        author_id=str(comment.author.mid),
        content=comment.content,
        published_at=_format_timestamp(comment.ctime),
        like_count=comment.likes,
        ip_location=comment.location,
    )


def _child_row(
    comment: Comment,
    root_id: str,
    root_sequence: int,
    root_author: tuple[str, str],
    known_authors: dict[str, tuple[str, str]],
) -> CsvRow:
    parent_id = str(comment.parent) if comment.parent else root_id
    replied_to = _reply_to_author_pair(
        getattr(comment, "reply_to_author", None)
    )
    if replied_to is None:
        replied_to = known_authors.get(parent_id)
    if replied_to is None and parent_id == root_id:
        replied_to = root_author
    if replied_to is None:
        replied_to_id: str | None = None
        replied_to_name: str | None = None
    else:
        replied_to_id, replied_to_name = replied_to
    return CsvRow(
        root_sequence=root_sequence,
        relation="二级评论",
        comment_id=str(comment.rpid),
        root_id=root_id,
        parent_id=parent_id,
        replied_to_name=replied_to_name,
        replied_to_id=replied_to_id,
        author_name=comment.author.name,
        author_id=str(comment.author.mid),
        content=comment.content,
        published_at=_format_timestamp(comment.ctime),
        like_count=comment.likes,
        ip_location=comment.location,
    )


def _reply_to_author_pair(value: object) -> tuple[str, str] | None:
    """Read a future ``Comment.reply_to_author`` without coupling its type."""

    if value is None:
        return None
    raw_id = getattr(value, "mid", None)
    if raw_id is None:
        raw_id = getattr(value, "user_id", None)
    raw_name = getattr(value, "name", None)
    if raw_name is None:
        raw_name = getattr(value, "uname", None)
    author_id = "" if raw_id is None else str(raw_id)
    author_name = "" if raw_name is None else str(raw_name)
    if not author_id and not author_name:
        return None
    return author_id, author_name


def _format_timestamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, _CHINA_TIMEZONE).isoformat(
        sep=" ", timespec="seconds"
    )


def _child_page_number(value: object | None) -> int:
    if value is None:
        return 1
    if isinstance(value, bool):
        raise CrawlStateError("二级评论页码格式无效")
    try:
        page_no = int(value)
    except (TypeError, ValueError) as exc:
        raise CrawlStateError(f"二级评论页码格式无效: {value!r}") from exc
    if page_no < 1:
        raise CrawlStateError(f"二级评论页码必须大于 0: {page_no}")
    return page_no


def _child_detail_cursor(value: object | None) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise CrawlStateError("二级评论明细游标格式无效")
    try:
        cursor = int(value)
    except (TypeError, ValueError) as exc:
        raise CrawlStateError(
            f"二级评论明细游标格式无效: {value!r}"
        ) from exc
    if cursor < 0 or str(value).strip() != str(cursor):
        raise CrawlStateError(
            f"二级评论明细游标必须是非负整数: {value!r}"
        )
    return cursor


def _child_cursor(value: object | None, strategy: str) -> int:
    if strategy == "page":
        return _child_page_number(value)
    if strategy == "detail":
        return _child_detail_cursor(value)
    raise CrawlStateError(f"未知的二级评论抓取策略: {strategy!r}")


def _cursor_key(cursor: object | None) -> str:
    try:
        return json.dumps(
            cursor,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return repr(cursor)


def _append_unique(items: list[object], value: str) -> None:
    if value not in {str(item) for item in items}:
        items.append(value)


def _count_ip_locations(path: Path) -> int | None:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            return sum(1 for row in reader if (row.get("IP属地") or "").strip())
    except (OSError, csv.Error):
        return None

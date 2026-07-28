from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from bili_comments.crawler import CrawlStateError, Crawler
from bili_comments.models import Author, Comment, CommentPage, VideoInfo
from bili_comments.storage import (
    CheckpointError,
    CheckpointStore,
    CsvRow,
    CsvStorageError,
    CsvStore,
    StorageError,
    TaskLock,
    escape_excel_text,
    unescape_excel_text,
)


BVID = "BV0000000000"
SYNTHETIC_AID = 10**30
VIDEO = VideoInfo(
    aid=SYNTHETIC_AID,
    bvid=BVID,
    title="可靠性测试",
    owner=Author(mid="synthetic-owner", name="测试 UP"),
)


def make_comment(
    rpid: str,
    *,
    root: str | None = None,
    parent: str | None = None,
    rcount: int = 0,
    reply_to_author: Author | None = None,
) -> Comment:
    return Comment(
        rpid=rpid,
        root=root or rpid,
        parent=parent,
        author=Author(
            mid=f"synthetic-author-{rpid}",
            name=f"作者{rpid}",
        ),
        content=f"评论 {rpid}",
        ctime=1_700_000_000,
        likes=1,
        location="IP属地：测试地区",
        rcount=rcount,
        reply_to_author=reply_to_author,
    )


def make_csv_row(
    comment_id: str,
    *,
    relation: str = "一级评论",
    root_id: str | None = None,
    parent_id: str | None = None,
    sequence: int = 1,
    author_name: str | None = None,
    replied_to_name: str | None = "UP",
    content: str = "正文",
    ip_location: str | None = "IP属地：测试地区",
) -> CsvRow:
    resolved_root = root_id or comment_id
    return CsvRow(
        root_sequence=sequence,
        relation=relation,
        comment_id=comment_id,
        root_id=resolved_root,
        parent_id=(
            parent_id
            if relation == "二级评论"
            else None
        ),
        replied_to_name=replied_to_name,
        replied_to_id=(
            "synthetic-replied-to"
            if replied_to_name is not None
            else None
        ),
        author_name=author_name or f"作者{comment_id}",
        author_id=f"synthetic-author-{comment_id}",
        content=content,
        published_at="2026-07-27 16:00:00+08:00",
        like_count=1,
        ip_location=ip_location,
    )


class EmptyClient:
    cookie = None

    def __init__(self) -> None:
        self.root_calls: list[object] = []
        self.child_calls: list[tuple[str, int]] = []
        self.detail_calls: list[tuple[str, int]] = []

    def fetch_root_page(
        self,
        video: VideoInfo,
        cursor: object | None,
    ) -> CommentPage:
        self.root_calls.append(cursor)
        return CommentPage([], None, False)

    def fetch_child_page(
        self,
        video: VideoInfo,
        root_id: str,
        page_no: int,
    ) -> CommentPage:
        self.child_calls.append((root_id, page_no))
        raise AssertionError("child endpoint must not be called")

    def fetch_child_detail_page(
        self,
        video: VideoInfo,
        root_id: str,
        next_cursor: int,
    ) -> CommentPage:
        self.detail_calls.append((root_id, next_cursor))
        raise AssertionError("child detail endpoint must not be called")


class ReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output_dir = self.root / "output"
        self.state_dir = self.root / "state"
        self.csv_path = self.output_dir / f"{BVID}.csv"
        self.state_path = self.state_dir / f"{BVID}.json"

    def read_rows(self) -> list[dict[str, str]]:
        with self.csv_path.open(
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as source:
            return list(csv.DictReader(source))

    def test_crawler_lock_blocks_concurrent_restart(self) -> None:
        client = EmptyClient()
        lock_path = self.state_dir / f"{BVID}.lock"
        with TaskLock(lock_path):
            with self.assertRaisesRegex(StorageError, "already running"):
                Crawler(
                    client,
                    output_dir=self.output_dir,
                    state_dir=self.state_dir,
                ).run(VIDEO, restart=True)
        self.assertEqual(client.root_calls, [])
        self.assertFalse(self.csv_path.exists())

    def test_task_lock_rejects_symbolic_link_without_touching_target(self) -> None:
        self.state_dir.mkdir(parents=True)
        target = self.root / "synthetic-target.txt"
        target.write_text("unchanged\n", encoding="utf-8")
        lock_path = self.state_dir / f"{BVID}.lock"
        lock_path.symlink_to(target)

        with self.assertRaisesRegex(StorageError, "failed to acquire task lock"):
            with TaskLock(lock_path):
                self.fail("symbolic-link lock must not be acquired")

        self.assertEqual(target.read_text(encoding="utf-8"), "unchanged\n")

    def test_header_only_orphan_csv_rebuilds_initial_checkpoint(self) -> None:
        csv_store = CsvStore(self.csv_path)
        self.assertTrue(csv_store.is_header_only)

        result = Crawler(
            EmptyClient(),
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        ).run(VIDEO)

        self.assertEqual(result.total_count, 0)
        checkpoint = CheckpointStore(self.state_dir, BVID).load()
        assert checkpoint is not None
        self.assertEqual(checkpoint.schema_version, 3)
        self.assertEqual(checkpoint.auth_mode, "anonymous")
        self.assertEqual(checkpoint.status, "complete")

    def test_authenticated_and_anonymous_runs_cannot_share_checkpoint(self) -> None:
        Crawler(
            EmptyClient(),
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        ).run(VIDEO, auth_mode="anonymous")

        with self.assertRaisesRegex(CrawlStateError, "登录模式"):
            Crawler(
                EmptyClient(),
                output_dir=self.output_dir,
                state_dir=self.state_dir,
            ).run(VIDEO, auth_mode="authenticated")

    def test_checkpoint_counts_and_next_sequence_must_match_csv(self) -> None:
        Crawler(
            EmptyClient(),
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        ).run(VIDEO)
        value = json.loads(self.state_path.read_text(encoding="utf-8"))

        value["rows_written"] = 1
        self.state_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(CsvStorageError, "row count"):
            Crawler(
                EmptyClient(),
                output_dir=self.output_dir,
                state_dir=self.state_dir,
            ).run(VIDEO)

        value["rows_written"] = 0
        value["next_root_sequence"] = 2
        self.state_path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(CrawlStateError, "一级评论序号"):
            Crawler(
                EmptyClient(),
                output_dir=self.output_dir,
                state_dir=self.state_dir,
            ).run(VIDEO)

    def test_semantically_invalid_complete_checkpoint_is_rejected(self) -> None:
        Crawler(
            EmptyClient(),
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        ).run(VIDEO)
        value = json.loads(self.state_path.read_text(encoding="utf-8"))
        value["phase"] = "anything"
        value["current_root_id"] = "ghost"
        value["sub_cursor"] = -7
        self.state_path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaises(CheckpointError):
            CheckpointStore(self.state_dir, BVID).load()

    def test_invalid_committed_prefix_never_truncates_csv(self) -> None:
        store = CsvStore(self.csv_path)
        store.append_rows(
            [
                make_csv_row(
                    "100",
                    ip_location="Synthetic Region",
                )
            ]
        )
        original = self.csv_path.read_bytes()
        inside_last_field = len(original) - len(b"dong\r\n")

        with self.assertRaisesRegex(CsvStorageError, "record boundary"):
            CsvStore(
                self.csv_path,
                committed_bytes=inside_last_field,
                expected_rows=1,
            )

        self.assertEqual(self.csv_path.read_bytes(), original)

    def test_expected_rows_is_checked_before_uncommitted_tail_truncation(
        self,
    ) -> None:
        store = CsvStore(self.csv_path)
        store.append_rows([make_csv_row("100")])
        first_commit = store.committed_bytes
        store.append_rows([make_csv_row("200", sequence=2)])
        original = self.csv_path.read_bytes()

        with self.assertRaisesRegex(CsvStorageError, "row count"):
            CsvStore(
                self.csv_path,
                committed_bytes=first_commit,
                expected_rows=2,
            )

        self.assertEqual(self.csv_path.read_bytes(), original)

    def test_v1_auth_mode_migration_fails_closed_when_history_is_ambiguous(
        self,
    ) -> None:
        scenarios = (
            ("ip_means_authenticated", "IP属地：测试地区", "anonymous"),
            ("no_ip_is_ambiguous", None, "authenticated"),
        )
        for name, ip_location, requested_mode in scenarios:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                csv_path = root / "output" / f"{BVID}.csv"
                state_path = root / "state" / f"{BVID}.json"
                csv_store = CsvStore(csv_path)
                csv_store.append_rows(
                    [
                        make_csv_row(
                            "100",
                            ip_location=ip_location,
                        )
                    ]
                )
                legacy = {
                    "schema_version": 1,
                    "bvid": BVID,
                    "aid": VIDEO.aid,
                    "status": "complete",
                    "phase": "complete",
                    "main_cursor": None,
                    "next_main_cursor": None,
                    "completed_root_ids_in_page": ["100"],
                    "current_root_id": None,
                    "sub_cursor": None,
                    "next_root_sequence": 2,
                    "rows_written": 1,
                    "committed_bytes": csv_store.committed_bytes,
                    "updated_at": "2026-07-27T16:00:00+08:00",
                }
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(json.dumps(legacy), encoding="utf-8")

                with self.assertRaisesRegex(CrawlStateError, "--restart"):
                    Crawler(
                        EmptyClient(),
                        output_dir=root / "output",
                        state_dir=root / "state",
                    ).run(VIDEO, auth_mode=requested_mode)

                unchanged = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(unchanged["schema_version"], 1)

    def test_empty_v1_checkpoint_can_adopt_current_auth_mode(self) -> None:
        csv_store = CsvStore(self.csv_path)
        legacy = {
            "schema_version": 1,
            "bvid": BVID,
            "aid": VIDEO.aid,
            "status": "complete",
            "phase": "complete",
            "main_cursor": None,
            "next_main_cursor": None,
            "completed_root_ids_in_page": [],
            "current_root_id": None,
            "sub_cursor": None,
            "next_root_sequence": 1,
            "rows_written": 0,
            "committed_bytes": csv_store.committed_bytes,
            "updated_at": "2026-07-27T16:00:00+08:00",
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(legacy), encoding="utf-8")

        result = Crawler(
            EmptyClient(),
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        ).run(VIDEO, auth_mode="authenticated")

        self.assertTrue(result.already_complete)
        migrated = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(migrated["auth_mode"], "authenticated")

    def test_legacy_unescaped_formula_is_rejected_without_migration(self) -> None:
        csv_store = CsvStore(self.csv_path)
        with self.csv_path.open("a", encoding="utf-8", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=tuple(make_csv_row("100").as_csv_dict()))
            writer.writerow(
                make_csv_row(
                    "100",
                    author_name="=HYPERLINK(\"https://example.invalid\")",
                ).as_csv_dict()
            )
            output.flush()
        original_csv = self.csv_path.read_bytes()
        legacy = {
            "schema_version": 1,
            "bvid": BVID,
            "aid": VIDEO.aid,
            "status": "complete",
            "phase": "complete",
            "main_cursor": None,
            "next_main_cursor": None,
            "completed_root_ids_in_page": ["100"],
            "current_root_id": None,
            "sub_cursor": None,
            "next_root_sequence": 2,
            "rows_written": 1,
            "committed_bytes": len(original_csv),
            "updated_at": "2026-07-27T16:00:00+08:00",
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(legacy), encoding="utf-8")

        with self.assertRaisesRegex(CsvStorageError, "Excel formula"):
            Crawler(
                EmptyClient(),
                output_dir=self.output_dir,
                state_dir=self.state_dir,
            ).run(VIDEO, auth_mode="authenticated")

        self.assertEqual(self.csv_path.read_bytes(), original_csv)
        unchanged = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(unchanged["schema_version"], 1)

    def test_v1_final_child_state_migrates_without_replaying_page_one(self) -> None:
        csv_store = CsvStore(self.csv_path)
        csv_store.append_rows(
            [
                make_csv_row("100"),
                make_csv_row(
                    "101",
                    relation="二级评论",
                    root_id="100",
                    parent_id="100",
                ),
            ]
        )
        legacy = {
            "schema_version": 1,
            "bvid": BVID,
            "aid": VIDEO.aid,
            "status": "running",
            "phase": "child_page",
            "main_cursor": None,
            "next_main_cursor": None,
            "completed_root_ids_in_page": [],
            "current_root_id": "100",
            "sub_cursor": None,
            "next_root_sequence": 2,
            "rows_written": 2,
            "committed_bytes": csv_store.committed_bytes,
            "updated_at": "2026-07-27T16:00:00+08:00",
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(legacy),
            encoding="utf-8",
        )
        root = make_comment("100", rcount=1)

        class ResumeClient(EmptyClient):
            def fetch_root_page(
                self,
                video: VideoInfo,
                cursor: object | None,
            ) -> CommentPage:
                self.root_calls.append(cursor)
                return CommentPage([root], None, False)

        client = ResumeClient()
        result = Crawler(
            client,
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        ).run(VIDEO, auth_mode="authenticated")

        self.assertEqual(result.total_count, 2)
        self.assertEqual(client.child_calls, [])
        migrated = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(migrated["auth_mode"], "authenticated")
        self.assertEqual(migrated["status"], "complete")

    def test_last_child_page_and_root_completion_are_one_commit(self) -> None:
        root = make_comment("100", rcount=1)
        child = make_comment("101", root="100", parent="100")

        class FirstClient(EmptyClient):
            def fetch_root_page(
                self,
                video: VideoInfo,
                cursor: object | None,
            ) -> CommentPage:
                self.root_calls.append(cursor)
                return CommentPage([root], None, False)

            def fetch_child_detail_page(
                self,
                video: VideoInfo,
                root_id: str,
                next_cursor: int,
            ) -> CommentPage:
                self.detail_calls.append((root_id, next_cursor))
                return CommentPage([child], None, False)

        def interrupt_after_child_commit(message: str) -> None:
            if message.startswith("二级评论明细"):
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            Crawler(
                FirstClient(),
                output_dir=self.output_dir,
                state_dir=self.state_dir,
                progress=interrupt_after_child_commit,
            ).run(VIDEO)

        interrupted = CheckpointStore(self.state_dir, BVID).load()
        assert interrupted is not None
        self.assertEqual(interrupted.phase, "root_page")
        self.assertIsNone(interrupted.current_root_id)
        self.assertIn("100", interrupted.completed_root_ids_in_page)
        self.assertEqual(interrupted.child_strategy, "page")

        class ResumeClient(FirstClient):
            def fetch_child_detail_page(
                self,
                video: VideoInfo,
                root_id: str,
                next_cursor: int,
            ) -> CommentPage:
                self.detail_calls.append((root_id, next_cursor))
                raise AssertionError("completed child stream must not replay")

        resumed = ResumeClient()
        result = Crawler(
            resumed,
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        ).run(VIDEO)
        self.assertEqual(result.total_count, 2)
        self.assertEqual(resumed.child_calls, [])

    def test_unknown_parent_is_blank_but_explicit_reply_author_wins(self) -> None:
        root = make_comment("100", rcount=2)
        unknown = make_comment("101", root="100", parent="999")
        explicit = make_comment(
            "102",
            root="100",
            parent="998",
            reply_to_author=Author(
                mid="synthetic-explicit-target",
                name="明确目标",
            ),
        )

        class RelationshipClient(EmptyClient):
            def fetch_root_page(
                self,
                video: VideoInfo,
                cursor: object | None,
            ) -> CommentPage:
                return CommentPage([root], None, False)

            def fetch_child_detail_page(
                self,
                video: VideoInfo,
                root_id: str,
                next_cursor: int,
            ) -> CommentPage:
                return CommentPage([unknown, explicit], None, False)

        Crawler(
            RelationshipClient(),
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        ).run(VIDEO)
        by_id = {row["评论ID"]: row for row in self.read_rows()}
        self.assertEqual(by_id["101"]["被评论者ID"], "")
        self.assertEqual(by_id["101"]["被评论者昵称"], "")
        self.assertEqual(
            by_id["102"]["被评论者ID"],
            "synthetic-explicit-target",
        )
        self.assertEqual(by_id["102"]["被评论者昵称"], "明确目标")

    def test_strict_csv_rejects_unclosed_record_and_duplicate_ids(self) -> None:
        store = CsvStore(self.csv_path)
        with self.csv_path.open("ab") as output:
            output.write(
                '1,一级评论,1,1,,up,9,user,10,"unterminated\n'.encode(
                    "utf-8"
                )
            )
        with self.assertRaises(CsvStorageError):
            CsvStore(self.csv_path)

        self.csv_path.unlink()
        first = CsvStore(self.csv_path)
        stale = CsvStore(self.csv_path)
        row = make_csv_row("1")
        first.append_rows([row])
        stale.append_rows([row])
        with self.assertRaisesRegex(CsvStorageError, "duplicate comment ID"):
            CsvStore(self.csv_path)

    def test_excel_formula_escape_is_safe_and_reversible(self) -> None:
        values = [
            "=HYPERLINK(\"https://example.invalid\")",
            "+SUM(1,1)",
            "-1+2",
            "@SUM(1,1)",
            "\n=SUM(1,1)",
            "'=literal",
            "普通文本",
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(
                    unescape_excel_text(escape_excel_text(value)),
                    value,
                )

        store = CsvStore(self.csv_path)
        store.append_rows(
            [
                make_csv_row(
                    "1",
                    author_name="=HYPERLINK(\"https://example.invalid\")",
                    replied_to_name="'+原始引号",
                    content="@SUM(1,1)",
                )
            ]
        )
        physical = self.read_rows()[0]
        self.assertTrue(physical["评论者昵称"].startswith("'="))
        self.assertTrue(physical["被评论者昵称"].startswith("''"))
        self.assertTrue(physical["评论内容"].startswith("'@"))

        reopened = CsvStore(
            self.csv_path,
            committed_bytes=store.committed_bytes,
        )
        self.assertEqual(
            reopened.authors["1"],
            (
                "synthetic-author-1",
                '=HYPERLINK("https://example.invalid")',
            ),
        )

        self.csv_path.unlink()
        all_text_store = CsvStore(self.csv_path)
        all_text_row = make_csv_row("=comment").as_csv_dict()
        all_text_row.update(
            {
                "根评论ID": "=comment",
                "被评论者ID": "+target",
                "评论者用户ID": "-author",
                "发布时间": "\n=NOW()",
                "IP属地": "\t=LOCATION()",
            }
        )
        all_text_store.append_rows([all_text_row])
        physical = self.read_rows()[0]
        for column in (
            "评论ID",
            "根评论ID",
            "被评论者ID",
            "评论者用户ID",
            "发布时间",
            "IP属地",
        ):
            with self.subTest(column=column):
                self.assertTrue(physical[column].startswith("'"))
        reopened = CsvStore(self.csv_path)
        self.assertIn("=comment", reopened.seen_ids)
        self.assertEqual(reopened.authors["=comment"][0], "-author")


if __name__ == "__main__":
    unittest.main()

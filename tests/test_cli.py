from __future__ import annotations

import csv
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bili_comments.api import ChildPaginationLimitError
from bili_comments.cli import (
    CookieConfigurationError,
    load_cookie,
    main,
    save_cookie,
)
from bili_comments.crawler import CrawlResult, Crawler
from bili_comments.models import Author, Comment, CommentPage, VideoInfo
from bili_comments.storage import CheckpointStore


BVID = "BV0000000000"
SYNTHETIC_AID = 10**30
SYNTHETIC_OWNER_ID = "synthetic-owner"
EXPLICIT_COOKIE = "test_session=explicit-placeholder"
FALLBACK_COOKIE = "test_session=fallback-placeholder"
ENVIRONMENT_COOKIE = "test_session=environment-placeholder"
PRIVATE_COOKIE_MARKER = "never-log-this-test-marker"
PRIVATE_COOKIE = (
    f"test_session={PRIVATE_COOKIE_MARKER}; test_csrf=placeholder"
)
VIDEO = VideoInfo(
    aid=SYNTHETIC_AID,
    bvid=BVID,
    title="测试视频",
    owner=Author(mid=SYNTHETIC_OWNER_ID, name="测试 UP"),
    reply_count=6,
)


def comment(
    rpid: str,
    *,
    root: str | None = None,
    parent: str | None = None,
    name: str | None = None,
    mid: str | None = None,
    rcount: int = 0,
    location: str | None = "IP属地：测试地区",
) -> Comment:
    return Comment(
        rpid=rpid,
        root=root or rpid,
        parent=parent,
        author=Author(
            mid=mid or f"synthetic-author-{rpid}",
            name=name or f"作者{rpid}",
        ),
        content=f"评论 {rpid}",
        ctime=1_700_000_000,
        likes=3,
        location=location,
        rcount=rcount,
    )


class ScriptedClient:
    def __init__(
        self,
        root_pages: dict[object, CommentPage | BaseException],
        child_pages: dict[tuple[str, int], CommentPage | BaseException],
        detail_pages: (
            dict[tuple[str, int], CommentPage | BaseException] | None
        ) = None,
    ) -> None:
        self.root_pages = root_pages
        self.child_pages = child_pages
        self.detail_pages = child_pages if detail_pages is None else detail_pages
        self.root_calls: list[object] = []
        self.child_calls: list[tuple[str, int]] = []
        self.detail_calls: list[tuple[str, int]] = []

    def fetch_root_page(
        self, video: VideoInfo, cursor: object | None
    ) -> CommentPage:
        self.root_calls.append(cursor)
        result = self.root_pages[cursor]
        if isinstance(result, BaseException):
            raise result
        return result

    def fetch_child_detail_page(
        self,
        video: VideoInfo,
        root_id: str,
        next_cursor: int,
    ) -> CommentPage:
        key = (root_id, next_cursor)
        self.detail_calls.append(key)
        result = self.detail_pages[key]
        if isinstance(result, BaseException):
            raise result
        return result

    def fetch_child_page(
        self, video: VideoInfo, root_id: str, page_no: int
    ) -> CommentPage:
        key = (root_id, page_no)
        self.child_calls.append(key)
        result = self.child_pages[key]
        if isinstance(result, BaseException):
            raise result
        return result


class CrawlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output_dir = self.root / "output"
        self.state_dir = self.root / "state"

    def read_rows(self) -> list[dict[str, str]]:
        path = self.output_dir / f"{BVID}.csv"
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            return list(csv.DictReader(source))

    def test_crawls_multiple_root_and_child_pages_with_relationships(self) -> None:
        root_100 = comment("100", name="根作者", rcount=2)
        root_200 = comment("200", rcount=0, location=None)
        root_300 = comment("300", rcount=1)
        child_101 = comment(
            "101",
            root="100",
            parent="100",
            name="子作者一",
        )
        child_102 = comment(
            "102",
            root="100",
            parent="101",
            name="子作者二",
        )
        child_301 = comment(
            "301",
            root="300",
            parent="300",
            location=None,
        )
        client = ScriptedClient(
            {
                None: CommentPage(
                    [root_100, root_200],
                    next_cursor="next",
                    has_more=True,
                ),
                "next": CommentPage(
                    [root_200, root_300],
                    next_cursor=None,
                    has_more=False,
                ),
            },
            {
                ("100", 0): CommentPage(
                    [child_101, child_102],
                    next_cursor=None,
                    has_more=False,
                ),
                ("300", 0): CommentPage(
                    [child_301],
                    next_cursor=None,
                    has_more=False,
                ),
            },
        )
        messages: list[str] = []
        crawler = Crawler(
            client,
            output_dir=self.output_dir,
            state_dir=self.state_dir,
            progress=messages.append,
        )

        result = crawler.run(VIDEO)

        self.assertEqual(result.root_count, 3)
        self.assertEqual(result.child_count, 3)
        self.assertEqual(result.total_count, 6)
        self.assertEqual(result.ip_location_count, 4)
        self.assertEqual(client.root_calls, [None, "next"])
        self.assertEqual(client.child_calls, [])
        self.assertEqual(client.detail_calls, [("100", 0), ("300", 0)])
        rows = self.read_rows()
        self.assertEqual(len(rows), 6)
        self.assertEqual(len({row["评论ID"] for row in rows}), 6)

        by_id = {row["评论ID"]: row for row in rows}
        self.assertEqual(by_id["100"]["被评论者昵称"], "测试 UP")
        self.assertEqual(by_id["102"]["父评论ID"], "101")
        self.assertEqual(by_id["102"]["被评论者昵称"], "子作者一")
        self.assertEqual(
            by_id["102"]["一级评论序号"],
            by_id["100"]["一级评论序号"],
        )
        self.assertIn("+08:00", by_id["100"]["发布时间"])
        self.assertTrue(any("累计 3 条" in message for message in messages))

        second_client = ScriptedClient({}, {})
        second_result = Crawler(
            second_client,
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        ).run(VIDEO)
        self.assertTrue(second_result.already_complete)
        self.assertEqual(second_client.root_calls, [])
        self.assertEqual(second_client.child_calls, [])
        self.assertEqual(second_client.detail_calls, [])

    def test_resumes_inside_child_pagination_without_duplicate_rows(self) -> None:
        root = comment("400", rcount=21)
        first_child = comment("401", root="400", parent="400")
        second_child = comment("402", root="400", parent="401")
        interrupted = ScriptedClient(
            {
                None: CommentPage(
                    [root],
                    next_cursor=None,
                    has_more=False,
                )
            },
            {
                ("400", 0): CommentPage(
                    [first_child],
                    next_cursor=20,
                    has_more=True,
                ),
                ("400", 20): KeyboardInterrupt(),
            },
        )
        crawler = Crawler(
            interrupted,
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        )
        with self.assertRaises(KeyboardInterrupt):
            crawler.run(VIDEO)

        resumed = ScriptedClient(
            {
                None: CommentPage(
                    [root],
                    next_cursor=None,
                    has_more=False,
                )
            },
            {
                ("400", 20): CommentPage(
                    [second_child],
                    next_cursor=None,
                    has_more=False,
                )
            },
        )
        result = Crawler(
            resumed,
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        ).run(VIDEO)

        self.assertEqual(resumed.child_calls, [])
        self.assertEqual(resumed.detail_calls, [("400", 20)])
        self.assertEqual(result.total_count, 3)
        rows = self.read_rows()
        self.assertEqual(
            [row["评论ID"] for row in rows],
            ["400", "401", "402"],
        )
        self.assertEqual(len({row["评论ID"] for row in rows}), 3)

    def test_page_limit_switch_is_durable_and_detail_replay_deduplicates(
        self,
    ) -> None:
        root = comment("100", rcount=3)
        existing = comment("101", root="100", parent="100")
        new_one = comment("102", root="100", parent="100")
        new_two = comment("103", root="100", parent="102")

        Crawler(
            ScriptedClient(
                {None: CommentPage([root], None, False)},
                {
                    ("100", 0): CommentPage(
                        [existing],
                        None,
                        False,
                    )
                },
            ),
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        ).run(VIDEO)

        store = CheckpointStore(self.state_dir, BVID)
        checkpoint = store.load()
        assert checkpoint is not None
        checkpoint.status = "running"
        checkpoint.phase = "child_page"
        checkpoint.completed_root_ids_in_page = []
        checkpoint.current_root_id = "100"
        checkpoint.sub_cursor = 252
        checkpoint.child_strategy = "page"
        store.save(checkpoint)

        limited = ScriptedClient(
            {},
            {
                ("100", 252): ChildPaginationLimitError(
                    -400,
                    "max offset exceeded",
                    "https://api.bilibili.com/x/v2/reply/reply",
                )
            },
            detail_pages={},
        )

        def interrupt_after_switch(message: str) -> None:
            if "已切换明细游标" in message:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            Crawler(
                limited,
                output_dir=self.output_dir,
                state_dir=self.state_dir,
                progress=interrupt_after_switch,
            ).run(VIDEO)

        switched = store.load()
        assert switched is not None
        self.assertEqual(switched.phase, "child_page")
        self.assertEqual(switched.current_root_id, "100")
        self.assertEqual(switched.child_strategy, "detail")
        self.assertEqual(switched.sub_cursor, 0)
        self.assertEqual(limited.child_calls, [("100", 252)])
        self.assertEqual(limited.detail_calls, [])

        resumed = ScriptedClient(
            {None: CommentPage([root], None, False)},
            {},
            detail_pages={
                ("100", 0): CommentPage(
                    [existing, new_one],
                    20,
                    True,
                ),
                ("100", 20): CommentPage(
                    [new_two],
                    None,
                    False,
                ),
            },
        )
        result = Crawler(
            resumed,
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        ).run(VIDEO)

        self.assertEqual(result.total_count, 4)
        self.assertEqual(resumed.child_calls, [])
        self.assertEqual(
            resumed.detail_calls,
            [("100", 0), ("100", 20)],
        )
        rows = self.read_rows()
        self.assertEqual(
            [row["评论ID"] for row in rows],
            ["100", "101", "102", "103"],
        )
        self.assertEqual(len({row["评论ID"] for row in rows}), 4)

    def test_active_v2_page_checkpoint_migrates_to_detail_zero(self) -> None:
        root = comment("200", rcount=2)
        existing = comment("201", root="200", parent="200")
        new_child = comment("202", root="200", parent="200")
        Crawler(
            ScriptedClient(
                {None: CommentPage([root], None, False)},
                {
                    ("200", 0): CommentPage(
                        [existing],
                        None,
                        False,
                    )
                },
            ),
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        ).run(VIDEO)

        state_path = self.state_dir / f"{BVID}.json"
        legacy = json.loads(state_path.read_text(encoding="utf-8"))
        legacy["schema_version"] = 2
        legacy.pop("child_strategy")
        legacy["status"] = "running"
        legacy["phase"] = "child_page"
        legacy["completed_root_ids_in_page"] = []
        legacy["current_root_id"] = "200"
        legacy["sub_cursor"] = 252
        state_path.write_text(
            json.dumps(legacy),
            encoding="utf-8",
        )

        resumed = ScriptedClient(
            {None: CommentPage([root], None, False)},
            {},
            detail_pages={
                ("200", 0): CommentPage(
                    [existing, new_child],
                    None,
                    False,
                )
            },
        )
        result = Crawler(
            resumed,
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        ).run(VIDEO)

        self.assertEqual(result.total_count, 3)
        self.assertEqual(resumed.child_calls, [])
        self.assertEqual(resumed.detail_calls, [("200", 0)])
        migrated = CheckpointStore(self.state_dir, BVID).load()
        assert migrated is not None
        self.assertEqual(migrated.schema_version, 3)
        self.assertEqual(migrated.status, "complete")
        self.assertEqual(migrated.child_strategy, "page")

    def test_restart_backs_up_complete_task_and_recrawls(self) -> None:
        initial = ScriptedClient(
            {None: CommentPage([], None, False)},
            {},
        )
        crawler = Crawler(
            initial,
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        )
        crawler.run(VIDEO)

        restarted = ScriptedClient(
            {
                None: CommentPage(
                    [comment("500")],
                    None,
                    False,
                )
            },
            {},
        )
        result = Crawler(
            restarted,
            output_dir=self.output_dir,
            state_dir=self.state_dir,
        ).run(VIDEO, restart=True)

        self.assertEqual(result.total_count, 1)
        self.assertEqual(
            len(list(self.output_dir.glob(f"{BVID}.csv.bak.*"))),
            1,
        )
        self.assertEqual(
            len(list(self.state_dir.glob(f"{BVID}.json.bak.*"))),
            1,
        )


class CookieTests(unittest.TestCase):
    def test_cookie_source_precedence_and_anonymous_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            explicit = root / "explicit.txt"
            fallback = root / "fallback.txt"
            explicit.write_text(f"{EXPLICIT_COOKIE}\n", encoding="utf-8")
            fallback.write_text(f"{FALLBACK_COOKIE}\n", encoding="utf-8")

            value, source = load_cookie(
                anonymous=False,
                cookie_file=explicit,
                environ={"BILI_COOKIE": ENVIRONMENT_COOKIE},
                fallback_path=fallback,
            )
            self.assertEqual((value, source), (EXPLICIT_COOKIE, "file"))

            value, source = load_cookie(
                anonymous=False,
                cookie_file=None,
                environ={"BILI_COOKIE": ENVIRONMENT_COOKIE},
                fallback_path=fallback,
            )
            self.assertEqual(
                (value, source),
                (ENVIRONMENT_COOKIE, "environment"),
            )

            self.assertEqual(
                load_cookie(
                    anonymous=True,
                    cookie_file=None,
                    environ={
                        "BILI_COOKIE": "test_session=ignored-placeholder"
                    },
                    fallback_path=fallback,
                ),
                (None, "anonymous"),
            )

    def test_missing_cookie_has_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.txt"
            with self.assertRaisesRegex(
                CookieConfigurationError,
                "python -m bili_comments auth",
            ):
                load_cookie(
                    anonymous=False,
                    cookie_file=None,
                    environ={},
                    fallback_path=missing,
                )

    def test_auth_saves_cookie_with_owner_only_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "nested" / "cookie.txt"
            out = io.StringIO()
            err = io.StringIO()

            exit_code = main(
                ["auth", "--cookie-file", str(destination)],
                getpass_fn=lambda _: PRIVATE_COOKIE,
                stdout=out,
                stderr=err,
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                destination.read_text(encoding="utf-8").strip(),
                PRIVATE_COOKIE,
            )
            self.assertEqual(
                stat.S_IMODE(destination.stat().st_mode),
                0o600,
            )
            self.assertNotIn(PRIVATE_COOKIE_MARKER, out.getvalue())
            self.assertEqual(err.getvalue(), "")

    @unittest.skipUnless(os.name == "posix", "POSIX atomic replace contract")
    def test_save_cookie_does_not_chmod_a_swapped_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "cookie.txt"
            target = root / "synthetic-target.txt"
            target.write_text("unchanged\n", encoding="utf-8")
            target.chmod(0o644)
            original_replace = os.replace

            def replace_then_swap(source: object, target_path: object) -> None:
                original_replace(source, target_path)
                Path(target_path).unlink()
                Path(target_path).symlink_to(target)

            with mock.patch(
                "bili_comments.cli.os.replace",
                side_effect=replace_then_swap,
            ):
                save_cookie(PRIVATE_COOKIE, destination)

            self.assertTrue(destination.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), "unchanged\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)


class CliTests(unittest.TestCase):
    def test_interactive_anonymous_crawl_and_summary(self) -> None:
        captured: dict[str, object] = {}
        result = CrawlResult(
            bvid=BVID,
            csv_path=Path("output") / f"{BVID}.csv",
            state_path=Path("state") / f"{BVID}.json",
            root_count=2,
            child_count=3,
            total_count=5,
            ip_location_count=4,
        )

        class FakeClient:
            def __init__(self, cookie: str | None) -> None:
                captured["cookie"] = cookie

            def resolve_video(self, value: str) -> VideoInfo:
                captured["video_value"] = value
                return VIDEO

        class FakeCrawler:
            def __init__(self, client: object, *, progress: object) -> None:
                captured["crawler_client"] = client

            def run(
                self, video: VideoInfo, *, restart: bool = False
            ) -> CrawlResult:
                captured["restart"] = restart
                return result

        out = io.StringIO()
        err = io.StringIO()
        exit_code = main(
            ["--anonymous"],
            input_fn=lambda _: (
                "https://www.bilibili.com/video/"
                f"{BVID}/?spm_id_from=test"
            ),
            stdout=out,
            stderr=err,
            environ={},
            client_factory=FakeClient,
            crawler_factory=FakeCrawler,
        )

        self.assertEqual(exit_code, 0)
        self.assertIsNone(captured["cookie"])
        self.assertEqual(captured["restart"], False)
        self.assertIn("匿名模式", out.getvalue())
        self.assertIn("一级评论：2", out.getvalue())
        self.assertIn("覆盖率 80.00%", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_default_mode_requires_cookie_before_network(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "cookie.txt"
            with mock.patch(
                "bili_comments.cli.default_cookie_path",
                return_value=missing,
            ):
                exit_code = main(
                    [BVID],
                    stdout=out,
                    stderr=err,
                    environ={},
                    client_factory=lambda **_: self.fail(
                        "client must not be created without a cookie"
                    ),
                )

        self.assertEqual(exit_code, 2)
        self.assertIn("auth", err.getvalue())

    def test_keyboard_interrupt_preserves_resume_message(self) -> None:
        class InterruptingClient:
            def __init__(self, cookie: str | None) -> None:
                pass

            def resolve_video(self, value: str) -> VideoInfo:
                raise KeyboardInterrupt

        out = io.StringIO()
        err = io.StringIO()
        exit_code = main(
            ["--anonymous", BVID],
            stdout=out,
            stderr=err,
            client_factory=InterruptingClient,
        )

        self.assertEqual(exit_code, 130)
        self.assertIn("断点", err.getvalue())


if __name__ == "__main__":
    unittest.main()

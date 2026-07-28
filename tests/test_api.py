from __future__ import annotations

from collections.abc import Callable
from email.message import Message
from http.client import HTTPException, IncompleteRead
import json
import math
import time
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from bili_comments.api import (
    AuthenticationRequiredError,
    BilibiliClient,
    ChildPaginationLimitError,
    InvalidVideoInput,
    ResponseFormatError,
    TemporaryNetworkError,
    VideoUnavailableError,
    _SameOriginHTTPSRedirectHandler,
    _parse_retry_after,
    extract_bvid,
    sign_wbi,
)
from bili_comments.models import Author, CommentPage, VideoInfo


BVID = "BV0000000000"
SYNTHETIC_AID = 10**30
SYNTHETIC_OWNER_ID = "synthetic-owner"
SYNTHETIC_COOKIE = "test_session=not-a-credential"
VIDEO = VideoInfo(
    aid=SYNTHETIC_AID,
    bvid=BVID,
    title="测试视频",
    owner=Author(mid=SYNTHETIC_OWNER_ID, name="测试 UP"),
    reply_count=3,
)
IMG_KEY = "a" * 32
SUB_KEY = "b" * 32


class FakeResponse:
    def __init__(
        self,
        payload: object | None = None,
        *,
        read_error: BaseException | None = None,
    ) -> None:
        self.headers = Message()
        self.headers["Content-Type"] = "application/json; charset=utf-8"
        self._raw = json.dumps(
            {"code": 0, "data": {}} if payload is None else payload,
            ensure_ascii=False,
        ).encode("utf-8")
        self._read_error = read_error

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        if self._read_error is not None:
            raise self._read_error
        return self._raw


class ScriptedOpener:
    def __init__(self, *results: FakeResponse | BaseException) -> None:
        self.results = list(results)
        self.requests: list[Request] = []

    def open(self, request: Request, timeout: float | None = None) -> FakeResponse:
        self.requests.append(request)
        if not self.results:
            raise AssertionError(f"unexpected request: {request.full_url}")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def nav_payload(*, is_login: bool, code: int = 0) -> dict[str, object]:
    return {
        "code": code,
        "message": "0" if code == 0 else "账号未登录",
        "data": {
            "isLogin": is_login,
            "wbi_img": {
                "img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png",
                "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png",
            },
        },
    }


def view_payload() -> dict[str, object]:
    return {
        "code": 0,
        "data": {
            "aid": VIDEO.aid,
            "bvid": VIDEO.bvid,
            "title": VIDEO.title,
            "owner": {"mid": VIDEO.owner.mid, "uname": VIDEO.owner.name},
            "stat": {"reply": VIDEO.reply_count},
        },
    }


def reply(
    rpid: int,
    *,
    root: int = 0,
    parent: int = 0,
    rcount: int = 0,
    parent_reply_member: object | None = None,
    members: list[object] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "rpid": rpid,
        "root": root,
        "parent": parent,
        "member": {
            "mid": f"synthetic-author-{rpid}",
            "uname": f"作者{rpid}",
        },
        "content": {
            "message": f"评论 {rpid}",
            "members": [] if members is None else members,
        },
        "ctime": 1_700_000_000,
        "like": 3,
        "rcount": rcount,
        "reply_control": {"location": "IP属地：测试地区"},
    }
    if parent_reply_member is not None:
        result["parent_reply_member"] = parent_reply_member
    return result


def child_payload(
    replies: list[object],
    *,
    page_no: int = 1,
    page_size: int = 20,
    total: int | None = None,
) -> dict[str, object]:
    return {
        "code": 0,
        "data": {
            "replies": replies,
            "page": {
                "num": page_no,
                "size": page_size,
                "count": len(replies) if total is None else total,
            },
        },
    }


def child_detail_payload(
    replies: list[object],
    *,
    next_cursor: object = 20,
    is_end: object = False,
    root_rpid: object = 100,
) -> dict[str, object]:
    return {
        "code": 0,
        "data": {
            "root": {"rpid": root_rpid, "replies": replies},
            "cursor": {
                "next": next_cursor,
                "is_end": is_end,
            },
        },
    }


def terminal_root_payload() -> dict[str, object]:
    return {
        "code": 0,
        "data": {
            "replies": [],
            "cursor": {
                "is_end": True,
                "pagination_reply": {"next_offset": ""},
            },
        },
    }


def make_http_error(
    status: int,
    reason: str = "temporary",
    *,
    retry_after: str | None = None,
) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(
        "https://api.bilibili.com/test",
        status,
        reason,
        headers,
        None,
    )


def make_client(
    opener: ScriptedOpener,
    *,
    cookie: str | None = None,
    max_attempts: int = 1,
    sleeps: list[float] | None = None,
    clock: FakeClock | None = None,
    auth_validation_ttl: float = 60.0,
) -> BilibiliClient:
    sink = [] if sleeps is None else sleeps
    return BilibiliClient(
        cookie=cookie,
        opener=opener,
        min_interval=0,
        max_interval=0,
        max_attempts=max_attempts,
        auth_validation_ttl=auth_validation_ttl,
        sleep=sink.append,
        clock=time.monotonic if clock is None else clock,
    )


class InputAndSigningTests(unittest.TestCase):
    def test_extracts_bvid_from_id_and_standard_url(self) -> None:
        self.assertEqual(extract_bvid(BVID), BVID)
        self.assertEqual(
            extract_bvid(
                f"https://www.bilibili.com/video/{BVID}/?spm_id_from=test"
            ),
            BVID,
        )

    def test_rejects_non_bilibili_host(self) -> None:
        with self.assertRaises(InvalidVideoInput):
            extract_bvid(f"https://bilibili.com.example/video/{BVID}/")

    def test_wbi_signature_filters_values_and_is_deterministic(self) -> None:
        signed = sign_wbi(
            {
                "foo": "a!b(c)*",
                "space": "x y",
                "none": None,
                "flag": True,
            },
            "0123456789abcdef0123456789abcdef",
            timestamp=1_700_000_000,
        )
        self.assertEqual(signed["foo"], "abc")
        self.assertEqual(signed["flag"], "1")
        self.assertNotIn("none", signed)
        self.assertEqual(signed["wts"], 1_700_000_000)
        self.assertEqual(
            signed["w_rid"],
            "b886430d2f80b0a357150750551e803b",
        )

    def test_auth_validation_ttl_must_be_non_negative_and_finite(self) -> None:
        BilibiliClient(auth_validation_ttl=0)
        for value in (-1, math.nan, math.inf):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    BilibiliClient(auth_validation_ttl=value)


class RedirectSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        client = BilibiliClient(cookie=SYNTHETIC_COOKIE)
        handlers = [
            handler
            for handler in client._opener.handlers
            if isinstance(handler, _SameOriginHTTPSRedirectHandler)
        ]
        self.assertEqual(len(handlers), 1)
        self.handler = handlers[0]
        self.request = Request(
            "https://api.bilibili.com/source",
            headers={"Cookie": SYNTHETIC_COOKIE},
        )
        self.headers = Message()

    def test_default_handler_rejects_cross_origin_redirect(self) -> None:
        with self.assertRaisesRegex(HTTPError, "unsafe cross-origin"):
            self.handler.redirect_request(
                self.request,
                None,
                302,
                "Found",
                self.headers,
                "https://attacker.example/collect",
            )

    def test_default_handler_rejects_https_downgrade(self) -> None:
        with self.assertRaisesRegex(HTTPError, "HTTPS-downgrade"):
            self.handler.redirect_request(
                self.request,
                None,
                302,
                "Found",
                self.headers,
                "http://api.bilibili.com/collect",
            )

    def test_default_handler_allows_same_origin_https_redirect(self) -> None:
        redirected = self.handler.redirect_request(
            self.request,
            None,
            302,
            "Found",
            self.headers,
            "https://api.bilibili.com/next",
        )
        self.assertIsNotNone(redirected)
        assert redirected is not None
        self.assertEqual(redirected.full_url, "https://api.bilibili.com/next")
        self.assertEqual(redirected.get_header("Cookie"), SYNTHETIC_COOKIE)


class AuthenticationTests(unittest.TestCase):
    def test_invalid_configured_cookie_fails_instead_of_falling_back(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(nav_payload(is_login=False, code=-101))
        )
        client = make_client(opener, cookie=SYNTHETIC_COOKIE)

        with self.assertRaises(AuthenticationRequiredError):
            client.validate_authentication()

        self.assertEqual(len(opener.requests), 1)
        self.assertIsNone(client._wbi_mixin_key)

    def test_anonymous_validation_returns_false_and_caches_wbi_key(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(nav_payload(is_login=False, code=-101))
        )
        client = make_client(opener)

        self.assertFalse(client.validate_authentication())
        cached_key = client._get_wbi_mixin_key()

        self.assertEqual(len(cached_key), 32)
        self.assertEqual(len(opener.requests), 1)

    def test_valid_cookie_returns_true_and_reuses_nav_wbi_key(self) -> None:
        opener = ScriptedOpener(FakeResponse(nav_payload(is_login=True)))
        client = make_client(opener, cookie=SYNTHETIC_COOKIE)

        self.assertTrue(client.validate_authentication())
        cached_key = client._get_wbi_mixin_key()

        self.assertEqual(len(cached_key), 32)
        self.assertEqual(len(opener.requests), 1)

    def test_resolve_video_validates_cookie_before_view_and_reuses_cache(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(nav_payload(is_login=True)),
            FakeResponse(view_payload()),
        )
        client = make_client(opener, cookie=SYNTHETIC_COOKIE)

        video = client.resolve_video(BVID)
        client._get_wbi_mixin_key()

        self.assertEqual(video, VIDEO)
        self.assertEqual(len(opener.requests), 2)
        self.assertIn("/x/web-interface/nav", opener.requests[0].full_url)
        self.assertIn("/x/web-interface/view", opener.requests[1].full_url)

    def test_resolve_video_does_not_continue_after_invalid_cookie(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(nav_payload(is_login=False, code=-101))
        )
        client = make_client(opener, cookie=SYNTHETIC_COOKIE)

        with self.assertRaises(AuthenticationRequiredError):
            client.resolve_video(BVID)

        self.assertEqual(len(opener.requests), 1)

    def test_missing_login_flag_fails_closed(self) -> None:
        payload = nav_payload(is_login=True)
        assert isinstance(payload["data"], dict)
        del payload["data"]["isLogin"]
        opener = ScriptedOpener(FakeResponse(payload))
        client = make_client(opener, cookie=SYNTHETIC_COOKIE)

        with self.assertRaises(ResponseFormatError):
            client.validate_authentication()


class AuthenticationFreshnessTests(unittest.TestCase):
    @staticmethod
    def nav_request_count(opener: ScriptedOpener) -> int:
        return sum(
            "/x/web-interface/nav" in request.full_url
            for request in opener.requests
        )

    def test_root_request_reuses_auth_within_ttl_and_refreshes_at_expiry(
        self,
    ) -> None:
        clock = FakeClock()
        opener = ScriptedOpener(
            FakeResponse(nav_payload(is_login=True)),
            FakeResponse(terminal_root_payload()),
            FakeResponse(nav_payload(is_login=True)),
            FakeResponse(terminal_root_payload()),
            FakeResponse(nav_payload(is_login=True)),
            FakeResponse(nav_payload(is_login=True)),
            FakeResponse(terminal_root_payload()),
            FakeResponse(nav_payload(is_login=True)),
        )
        client = make_client(
            opener,
            cookie=SYNTHETIC_COOKIE,
            clock=clock,
            auth_validation_ttl=60,
        )

        client.fetch_root_page(VIDEO)
        self.assertEqual(self.nav_request_count(opener), 2)
        clock.advance(59)
        client.fetch_root_page(VIDEO)
        self.assertEqual(self.nav_request_count(opener), 3)

        clock.advance(60)
        client.fetch_root_page(VIDEO)
        self.assertEqual(self.nav_request_count(opener), 5)
        self.assertEqual(len(opener.requests), 8)

    def test_root_request_stops_when_refreshed_cookie_is_invalid(self) -> None:
        clock = FakeClock()
        opener = ScriptedOpener(
            FakeResponse(nav_payload(is_login=True)),
            FakeResponse(terminal_root_payload()),
            FakeResponse(nav_payload(is_login=True)),
            FakeResponse(nav_payload(is_login=False, code=-101)),
        )
        client = make_client(
            opener,
            cookie=SYNTHETIC_COOKIE,
            clock=clock,
            auth_validation_ttl=60,
        )

        client.fetch_root_page(VIDEO)
        clock.advance(60)

        with self.assertRaises(AuthenticationRequiredError):
            client.fetch_root_page(VIDEO)

        self.assertEqual(self.nav_request_count(opener), 3)
        self.assertEqual(len(opener.requests), 4)
        self.assertIsNone(client._auth_validated_at)

    def test_child_request_uses_the_same_auth_freshness_policy(self) -> None:
        clock = FakeClock()
        empty_child = child_payload([], page_no=1, total=0)
        opener = ScriptedOpener(
            FakeResponse(nav_payload(is_login=True)),
            FakeResponse(empty_child),
            FakeResponse(nav_payload(is_login=True)),
            FakeResponse(empty_child),
            FakeResponse(nav_payload(is_login=True)),
            FakeResponse(nav_payload(is_login=True)),
            FakeResponse(empty_child),
            FakeResponse(nav_payload(is_login=True)),
        )
        client = make_client(
            opener,
            cookie=SYNTHETIC_COOKIE,
            clock=clock,
            auth_validation_ttl=60,
        )

        client.fetch_child_page(VIDEO, "100", 1)
        self.assertEqual(self.nav_request_count(opener), 2)
        clock.advance(59)
        client.fetch_child_page(VIDEO, "100", 1)
        self.assertEqual(self.nav_request_count(opener), 3)

        clock.advance(60)
        client.fetch_child_page(VIDEO, "100", 1)
        self.assertEqual(self.nav_request_count(opener), 5)
        self.assertEqual(len(opener.requests), 8)

    def test_child_request_stops_when_refreshed_cookie_is_invalid(self) -> None:
        clock = FakeClock()
        opener = ScriptedOpener(
            FakeResponse(nav_payload(is_login=True)),
            FakeResponse(child_payload([], page_no=1, total=0)),
            FakeResponse(nav_payload(is_login=True)),
            FakeResponse(nav_payload(is_login=False, code=-101)),
        )
        client = make_client(
            opener,
            cookie=SYNTHETIC_COOKIE,
            clock=clock,
            auth_validation_ttl=60,
        )

        client.fetch_child_page(VIDEO, "100", 1)
        clock.advance(60)

        with self.assertRaises(AuthenticationRequiredError):
            client.fetch_child_page(VIDEO, "100", 1)

        self.assertEqual(self.nav_request_count(opener), 3)
        self.assertEqual(len(opener.requests), 4)


class PostPageAuthenticationTests(unittest.TestCase):
    @staticmethod
    def page_cases() -> tuple[
        tuple[
            str,
            dict[str, object],
            Callable[[BilibiliClient], CommentPage],
            int,
        ],
        ...,
    ]:
        return (
            (
                "root",
                terminal_root_payload(),
                lambda client: client.fetch_root_page(VIDEO),
                0,
            ),
            (
                "legacy child",
                child_payload(
                    [reply(101, root=100, parent=100)],
                    page_no=1,
                    total=1,
                ),
                lambda client: client.fetch_child_page(VIDEO, "100", 1),
                1,
            ),
            (
                "detail child",
                child_detail_payload(
                    [reply(101, root=100, parent=100)],
                    next_cursor=0,
                    is_end=True,
                ),
                lambda client: client.fetch_child_detail_page(
                    VIDEO,
                    "100",
                    0,
                ),
                1,
            ),
        )

    @staticmethod
    def nav_request_count(opener: ScriptedOpener) -> int:
        return sum(
            "/x/web-interface/nav" in request.full_url
            for request in opener.requests
        )

    def test_successful_comment_page_is_rejected_when_post_auth_fails_within_ttl(
        self,
    ) -> None:
        for name, payload, fetch_page, _ in self.page_cases():
            with self.subTest(endpoint=name):
                clock = FakeClock()
                opener = ScriptedOpener(
                    FakeResponse(nav_payload(is_login=True)),
                    FakeResponse(payload),
                    FakeResponse(nav_payload(is_login=False, code=-101)),
                )
                client = make_client(
                    opener,
                    cookie=SYNTHETIC_COOKIE,
                    clock=clock,
                    auth_validation_ttl=60,
                )
                client.validate_authentication()
                clock.advance(59)

                with self.assertRaises(AuthenticationRequiredError):
                    fetch_page(client)

                self.assertEqual(self.nav_request_count(opener), 2)
                self.assertEqual(len(opener.requests), 3)
                self.assertNotIn(
                    "/x/web-interface/nav",
                    opener.requests[1].full_url,
                )
                self.assertIn(
                    "/x/web-interface/nav",
                    opener.requests[2].full_url,
                )
                self.assertIsNone(client._auth_validated_at)

    def test_successful_post_auth_returns_each_comment_page_within_ttl(
        self,
    ) -> None:
        for name, payload, fetch_page, expected_items in self.page_cases():
            with self.subTest(endpoint=name):
                clock = FakeClock()
                opener = ScriptedOpener(
                    FakeResponse(nav_payload(is_login=True)),
                    FakeResponse(payload),
                    FakeResponse(nav_payload(is_login=True)),
                )
                client = make_client(
                    opener,
                    cookie=SYNTHETIC_COOKIE,
                    clock=clock,
                    auth_validation_ttl=60,
                )
                client.validate_authentication()
                clock.advance(59)

                page = fetch_page(client)

                self.assertEqual(len(page.items), expected_items)
                self.assertFalse(page.has_more)
                self.assertIsNone(page.next_cursor)
                self.assertEqual(self.nav_request_count(opener), 2)
                self.assertEqual(len(opener.requests), 3)


class RetryTests(unittest.TestCase):
    def test_retries_http_408_then_returns_json(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener(
            make_http_error(408, "Request Timeout"),
            FakeResponse({"code": 0, "data": {"ok": True}}),
        )
        client = make_client(
            opener,
            max_attempts=2,
            sleeps=sleeps,
        )

        payload = client._request_json(
            "https://api.bilibili.com/test",
            referer="https://www.bilibili.com/",
        )

        self.assertEqual(payload["data"], {"ok": True})
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(len(sleeps), 1)

    def test_exhausted_http_408_is_temporary_network_error(self) -> None:
        opener = ScriptedOpener(
            make_http_error(408, "Request Timeout"),
            make_http_error(408, "Request Timeout"),
        )
        client = make_client(opener, max_attempts=2)

        with self.assertRaises(TemporaryNetworkError) as caught:
            client._request_json(
                "https://api.bilibili.com/test",
                referer="https://www.bilibili.com/",
            )

        self.assertEqual(caught.exception.status, 408)
        self.assertEqual(caught.exception.attempts, 2)

    def test_non_finite_retry_after_uses_finite_backoff(self) -> None:
        for retry_after in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(retry_after=retry_after):
                self.assertIsNone(_parse_retry_after(retry_after))
                sleeps: list[float] = []
                opener = ScriptedOpener(
                    make_http_error(429, retry_after=retry_after),
                    FakeResponse({"code": 0, "data": {"ok": True}}),
                )
                client = make_client(
                    opener,
                    max_attempts=2,
                    sleeps=sleeps,
                )

                payload = client._request_json(
                    "https://api.bilibili.com/test",
                    referer="https://www.bilibili.com/",
                )

                self.assertEqual(payload["data"], {"ok": True})
                self.assertEqual(len(sleeps), 1)
                self.assertTrue(math.isfinite(sleeps[0]))
                self.assertGreaterEqual(sleeps[0], 1.0)
                self.assertLessEqual(sleeps[0], 1.5)

    def test_negative_retry_after_is_clamped_to_zero(self) -> None:
        sleeps: list[float] = []
        opener = ScriptedOpener(
            make_http_error(408, retry_after="-5"),
            FakeResponse({"code": 0, "data": {"ok": True}}),
        )
        client = make_client(
            opener,
            max_attempts=2,
            sleeps=sleeps,
        )

        payload = client._request_json(
            "https://api.bilibili.com/test",
            referer="https://www.bilibili.com/",
        )

        self.assertEqual(payload["data"], {"ok": True})
        self.assertEqual(sleeps, [0.0])

    def test_retry_after_at_or_below_limit_is_honored(self) -> None:
        for retry_after, expected in (("2.5", 2.5), ("120", 120.0)):
            with self.subTest(retry_after=retry_after):
                sleeps: list[float] = []
                opener = ScriptedOpener(
                    make_http_error(500, retry_after=retry_after),
                    FakeResponse({"code": 0, "data": {"ok": True}}),
                )
                client = make_client(
                    opener,
                    max_attempts=2,
                    sleeps=sleeps,
                )

                payload = client._request_json(
                    "https://api.bilibili.com/test",
                    referer="https://www.bilibili.com/",
                )

                self.assertEqual(payload["data"], {"ok": True})
                self.assertEqual(sleeps, [expected])

    def test_retry_after_over_limit_fails_without_sleep_or_retry(self) -> None:
        endpoint = "https://api.bilibili.com/test"
        for status in (408, 429, 500, 599):
            with self.subTest(status=status):
                sleeps: list[float] = []
                opener = ScriptedOpener(
                    make_http_error(status, retry_after="120.01"),
                    FakeResponse({"code": 0, "data": {"ok": True}}),
                )
                client = make_client(
                    opener,
                    max_attempts=2,
                    sleeps=sleeps,
                )

                with self.assertRaises(TemporaryNetworkError) as caught:
                    client._request_json(
                        endpoint,
                        referer="https://www.bilibili.com/",
                    )

                self.assertEqual(caught.exception.status, status)
                self.assertEqual(caught.exception.endpoint, endpoint)
                self.assertEqual(caught.exception.attempts, 1)
                self.assertEqual(sleeps, [])
                self.assertEqual(len(opener.requests), 1)

    def test_retries_incomplete_read_and_generic_http_exception(self) -> None:
        errors = (
            IncompleteRead(b'{"code":', 10),
            HTTPException("connection closed"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                opener = ScriptedOpener(
                    FakeResponse(read_error=error),
                    FakeResponse({"code": 0, "data": {"ok": True}}),
                )
                client = make_client(opener, max_attempts=2)

                payload = client._request_json(
                    "https://api.bilibili.com/test",
                    referer="https://www.bilibili.com/",
                )

                self.assertEqual(payload["data"], {"ok": True})
                self.assertEqual(len(opener.requests), 2)


class PaginationAndMappingTests(unittest.TestCase):
    def test_root_page_without_cursor_fails_closed_even_when_empty(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(nav_payload(is_login=False, code=-101)),
            FakeResponse({"code": 0, "data": {}}),
        )
        client = make_client(opener)

        with self.assertRaisesRegex(ResponseFormatError, "cursor is missing"):
            client.fetch_root_page(VIDEO)

    def test_root_page_requires_explicit_is_end(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(nav_payload(is_login=False, code=-101)),
            FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "replies": [],
                        "cursor": {
                            "pagination_reply": {"next_offset": "next"}
                        },
                    },
                }
            ),
        )
        client = make_client(opener)

        with self.assertRaisesRegex(ResponseFormatError, "is_end"):
            client.fetch_root_page(VIDEO)

    def test_root_page_deduplicates_pinned_comment_and_advances_cursor(self) -> None:
        root_one = reply(100, rcount=1)
        root_two = reply(200)
        opener = ScriptedOpener(
            FakeResponse(nav_payload(is_login=False, code=-101)),
            FakeResponse(
                {
                    "code": 0,
                    "data": {
                        "top_replies": [root_one],
                        "replies": [root_one, root_two],
                        "cursor": {
                            "is_end": False,
                            "pagination_reply": {"next_offset": "next"},
                        },
                    },
                }
            ),
        )
        client = make_client(opener)

        page = client.fetch_root_page(VIDEO)

        self.assertEqual([item.rpid for item in page.items], ["100", "200"])
        self.assertTrue(page.has_more)
        self.assertEqual(page.next_cursor, "next")
        query = parse_qs(urlsplit(opener.requests[1].full_url).query)
        self.assertIn("w_rid", query)
        self.assertIn("wts", query)

    def test_root_page_rejects_child_parent_or_mismatched_root(self) -> None:
        malformed_replies = (
            reply(100, parent=99),
            reply(100, root=99),
        )
        for malformed in malformed_replies:
            with self.subTest(
                parent=malformed["parent"],
                root=malformed["root"],
            ):
                opener = ScriptedOpener(
                    FakeResponse(nav_payload(is_login=False, code=-101)),
                    FakeResponse(
                        {
                            "code": 0,
                            "data": {
                                "replies": [malformed],
                                "cursor": {
                                    "is_end": True,
                                    "pagination_reply": {"next_offset": ""},
                                },
                            },
                        }
                    ),
                )
                client = make_client(opener)

                with self.assertRaisesRegex(
                    ResponseFormatError,
                    "parent/root identity",
                ):
                    client.fetch_root_page(VIDEO)

    def test_child_page_number_mismatch_fails_closed(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(
                child_payload(
                    [reply(101, root=100, parent=100)],
                    page_no=5,
                    total=100,
                )
            )
        )
        client = make_client(opener)

        with self.assertRaisesRegex(ResponseFormatError, "does not match"):
            client.fetch_child_page(VIDEO, "100", 1)

    def test_child_endpoints_reject_noncanonical_root_identifier(self) -> None:
        client = make_client(ScriptedOpener())

        with self.assertRaisesRegex(ValueError, "canonical decimal"):
            client.fetch_child_page(VIDEO, "00100", 1)
        with self.assertRaisesRegex(ValueError, "canonical decimal"):
            client.fetch_child_detail_page(VIDEO, "00100", 0)

    def test_child_page_rejects_mismatched_root_identity(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(
                child_payload(
                    [reply(101, root=999, parent=999)],
                    page_no=1,
                    total=1,
                )
            )
        )
        client = make_client(opener)

        with self.assertRaisesRegex(ResponseFormatError, "child comment root"):
            client.fetch_child_page(VIDEO, "100", 1)

    def test_zero_child_page_size_with_remaining_rows_fails_closed(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(
                child_payload([], page_no=1, page_size=0, total=1)
            )
        )
        client = make_client(opener)

        with self.assertRaisesRegex(ResponseFormatError, "size is zero"):
            client.fetch_child_page(VIDEO, "100", 1)

    def test_child_page_limit_error_requires_exact_endpoint_code_and_message(
        self,
    ) -> None:
        exact = make_client(
            ScriptedOpener(
                FakeResponse(
                    {"code": -400, "message": "max offset exceeded"}
                )
            )
        )
        with self.assertRaises(ChildPaginationLimitError):
            exact.fetch_child_page(VIDEO, "100", 252)

        for message in (
            "max offset exceeded ",
            "MAX OFFSET EXCEEDED",
            "other -400",
        ):
            with self.subTest(message=message):
                client = make_client(
                    ScriptedOpener(
                        FakeResponse({"code": -400, "message": message})
                    )
                )
                with self.assertRaises(VideoUnavailableError) as caught:
                    client.fetch_child_page(VIDEO, "100", 252)
                self.assertNotIsInstance(
                    caught.exception,
                    ChildPaginationLimitError,
                )

        detail = make_client(
            ScriptedOpener(
                FakeResponse(
                    {"code": -400, "message": "max offset exceeded"}
                )
            )
        )
        with self.assertRaises(VideoUnavailableError) as caught:
            detail.fetch_child_detail_page(VIDEO, "100", 0)
        self.assertNotIsInstance(
            caught.exception,
            ChildPaginationLimitError,
        )

    def test_child_detail_page_maps_replies_and_advances_cursor(self) -> None:
        opener = ScriptedOpener(
            FakeResponse(
                child_detail_payload(
                    [reply(101, root=100, parent=100)],
                    next_cursor=20,
                )
            )
        )
        client = make_client(opener)

        page = client.fetch_child_detail_page(VIDEO, "100", 0)

        self.assertEqual([item.rpid for item in page.items], ["101"])
        self.assertTrue(page.has_more)
        self.assertEqual(page.next_cursor, 20)
        query = parse_qs(urlsplit(opener.requests[0].full_url).query)
        self.assertEqual(query["root"], ["100"])
        self.assertEqual(query["next"], ["0"])
        self.assertEqual(query["ps"], ["20"])

    def test_child_detail_page_requires_replies_and_forward_cursor(self) -> None:
        cases = (
            (
                {
                    "code": 0,
                    "data": {
                        "root": {"rpid": 100},
                        "cursor": {"next": 20, "is_end": False},
                    },
                },
                "root.replies",
            ),
            (
                child_detail_payload([], next_cursor=None),
                "cursor.next",
            ),
            (
                child_detail_payload([], next_cursor=20),
                "did not advance",
            ),
        )
        for payload, message in cases:
            with self.subTest(message=message):
                client = make_client(
                    ScriptedOpener(FakeResponse(payload))
                )
                with self.assertRaisesRegex(ResponseFormatError, message):
                    client.fetch_child_detail_page(VIDEO, "100", 20)

    def test_child_detail_page_rejects_mismatched_root_identity(self) -> None:
        cases = (
            (
                child_detail_payload(
                    [reply(101, root=100, parent=100)],
                    root_rpid=999,
                ),
                "data.root.rpid",
            ),
            (
                child_detail_payload(
                    [reply(101, root=999, parent=999)],
                    root_rpid=100,
                ),
                "child comment root",
            ),
        )
        for payload, message in cases:
            with self.subTest(message=message):
                client = make_client(
                    ScriptedOpener(FakeResponse(payload))
                )
                with self.assertRaisesRegex(ResponseFormatError, message):
                    client.fetch_child_detail_page(VIDEO, "100", 0)

    def test_child_detail_terminal_page_allows_reset_or_missing_next(
        self,
    ) -> None:
        reset = child_detail_payload(
            [reply(101, root=100, parent=100)],
            next_cursor=0,
            is_end=True,
        )
        missing = child_detail_payload(
            [reply(101, root=100, parent=100)],
            is_end=True,
        )
        assert isinstance(missing["data"], dict)
        assert isinstance(missing["data"]["cursor"], dict)
        del missing["data"]["cursor"]["next"]

        for payload in (reset, missing):
            with self.subTest(payload=payload):
                client = make_client(
                    ScriptedOpener(FakeResponse(payload))
                )
                page = client.fetch_child_detail_page(
                    VIDEO,
                    "100",
                    6200,
                )

                self.assertFalse(page.has_more)
                self.assertIsNone(page.next_cursor)

    def test_maps_only_explicit_parent_reply_member(self) -> None:
        explicit = reply(
            102,
            root=100,
            parent=101,
            parent_reply_member={
                "mid": "synthetic-direct-parent",
                "name": "直接父评论作者",
            },
        )
        mention_only = reply(
            103,
            root=100,
            parent=100,
            members=[
                {
                    "mid": "synthetic-mentioned-member",
                    "uname": "正文中被提及者",
                }
            ],
        )
        opener = ScriptedOpener(
            FakeResponse(child_payload([explicit, mention_only], total=2))
        )
        client = make_client(opener)

        page = client.fetch_child_page(VIDEO, "100", 1)

        self.assertEqual(
            page.items[0].reply_to_author_id,
            "synthetic-direct-parent",
        )
        self.assertEqual(
            page.items[0].reply_to_author_name,
            "直接父评论作者",
        )
        self.assertIsNone(page.items[1].reply_to_author)
        self.assertIsNone(page.items[1].reply_to_author_id)

    def test_parent_reply_member_without_stable_id_is_not_guessed(self) -> None:
        raw = reply(
            102,
            root=100,
            parent=101,
            parent_reply_member={"name": "缺少 ID"},
        )
        opener = ScriptedOpener(
            FakeResponse(child_payload([raw], total=1))
        )
        client = make_client(opener)

        page = client.fetch_child_page(VIDEO, "100", 1)

        self.assertIsNone(page.items[0].reply_to_author)


if __name__ == "__main__":
    unittest.main()

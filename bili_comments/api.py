"""Bilibili JSON API adapter implemented with the Python standard library."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from http.client import HTTPException
import json
import math
import random
import re
import socket
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    Request,
    build_opener,
)

from .models import Author, Comment, CommentPage, VideoInfo


_VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
_NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
_ROOT_REPLY_URL = "https://api.bilibili.com/x/v2/reply/wbi/main"
_CHILD_REPLY_URL = "https://api.bilibili.com/x/v2/reply/reply"
_VIDEO_TYPE = 1
_ROOT_PAGE_MODE = 3
_CHILD_PAGE_SIZE = 20
_WBI_KEY_TTL_SECONDS = 6 * 60 * 60

_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$", re.IGNORECASE)
_VIDEO_PATH_RE = re.compile(
    r"(?:^|/)video/(?P<bvid>BV[0-9A-Za-z]{10})(?:/|$)",
    re.IGNORECASE,
)
_WBI_FILTER_RE = re.compile(r"[!'()*]")
_MIXIN_KEY_ENC_TAB = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
)

_AUTH_CODES = frozenset({-101, -111})
_RISK_CODES = frozenset({-799, -509, -412, -352, 12032})
_ACCESS_CODES = frozenset({-403, 12002, 12010, 12012, 12035})
_UNAVAILABLE_CODES = frozenset({-404, -400, 10003, 62002, 62004, 62012, 12022})


class _SameOriginHTTPSRedirectHandler(HTTPRedirectHandler):
    """Reject redirects that could disclose request credentials.

    ``urllib`` preserves ordinary headers, including ``Cookie``, while following
    redirects. Bilibili requests therefore only follow redirects that remain on
    the exact same HTTPS origin.
    """

    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Request | None:
        source_origin = _url_origin(req.full_url)
        target_origin = _url_origin(newurl)
        if (
            source_origin is None
            or target_origin is None
            or source_origin[0] != "https"
            or target_origin[0] != "https"
            or source_origin != target_origin
        ):
            raise HTTPError(
                req.full_url,
                code,
                "unsafe cross-origin or HTTPS-downgrade redirect blocked",
                headers,
                fp,
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class BilibiliError(Exception):
    """Base class for all expected adapter failures."""


class InvalidVideoInput(BilibiliError, ValueError):
    """The supplied value is not a supported BVID or Bilibili video URL."""


class BilibiliAPIError(BilibiliError):
    """A non-zero Bilibili JSON API business response."""

    def __init__(self, code: int, message: str, endpoint: str | None = None) -> None:
        self.code = code
        self.message = message or "Bilibili API request failed"
        self.endpoint = endpoint
        suffix = f" ({endpoint})" if endpoint else ""
        super().__init__(f"Bilibili API {code}: {self.message}{suffix}")


class AuthenticationRequiredError(BilibiliAPIError):
    """The endpoint requires a valid logged-in Cookie."""


class AccessDeniedError(BilibiliAPIError):
    """The current account cannot access this video or comment area."""


class VideoUnavailableError(BilibiliAPIError):
    """The requested video is invalid, unavailable, or no longer public."""


class RiskControlError(BilibiliAPIError):
    """Bilibili stopped the request due to throttling or risk control."""


class TemporaryNetworkError(BilibiliError):
    """Retryable transport or HTTP failures exhausted the retry budget."""

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        status: int | None = None,
        attempts: int | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.status = status
        self.attempts = attempts
        details: list[str] = []
        if status is not None:
            details.append(f"HTTP {status}")
        if attempts is not None:
            details.append(f"{attempts} attempts")
        if endpoint:
            details.append(endpoint)
        suffix = f" ({', '.join(details)})" if details else ""
        super().__init__(f"{message}{suffix}")


class ResponseFormatError(BilibiliError):
    """The endpoint returned JSON whose shape no longer matches the adapter."""

    def __init__(self, message: str, endpoint: str | None = None) -> None:
        self.endpoint = endpoint
        suffix = f" ({endpoint})" if endpoint else ""
        super().__init__(f"{message}{suffix}")


def extract_bvid(value: str) -> str:
    """Extract and validate a BVID without making a network request."""

    if not isinstance(value, str):
        raise InvalidVideoInput("video must be a BVID or Bilibili video URL")

    candidate = value.strip()
    if not candidate:
        raise InvalidVideoInput("video cannot be empty")

    if _BVID_RE.fullmatch(candidate):
        return "BV" + candidate[2:]

    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise InvalidVideoInput("expected a BVID or an http(s) Bilibili video URL")

    host = (parsed.hostname or "").lower().rstrip(".")
    if host != "bilibili.com" and not host.endswith(".bilibili.com"):
        raise InvalidVideoInput(f"unsupported video host: {host or '(missing)'}")

    match = _VIDEO_PATH_RE.search(parsed.path)
    if match is None:
        raise InvalidVideoInput("Bilibili URL does not contain a /video/BV... path")
    bvid = match.group("bvid")
    return "BV" + bvid[2:]


def make_mixin_key(img_key: str, sub_key: str) -> str:
    """Derive the 32-character WBI mixin key from nav image keys."""

    source = f"{img_key}{sub_key}"
    if len(source) < 64:
        raise ResponseFormatError("WBI image keys are shorter than expected", _NAV_URL)
    return "".join(source[index] for index in _MIXIN_KEY_ENC_TAB)[:32]


def sign_wbi(
    params: Mapping[str, object],
    mixin_key: str,
    *,
    timestamp: int | None = None,
) -> dict[str, str | int]:
    """Return a new parameter mapping containing ``wts`` and ``w_rid``."""

    signed: dict[str, str | int] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            text = "1" if value else "0"
        else:
            text = str(value)
        signed[str(key)] = _WBI_FILTER_RE.sub("", text)

    signed["wts"] = int(time.time()) if timestamp is None else int(timestamp)
    canonical_query = urlencode(sorted(signed.items()))
    signed["w_rid"] = hashlib.md5(
        f"{canonical_query}{mixin_key}".encode("utf-8")
    ).hexdigest()
    return signed


class BilibiliClient:
    """Small serial client for video metadata and first/second-level comments."""

    def __init__(
        self,
        cookie: str | None = None,
        *,
        timeout: float = 15.0,
        min_interval: float = 1.5,
        max_interval: float = 3.0,
        max_attempts: int = 5,
        auth_validation_ttl: float = 60.0,
        opener: OpenerDirector | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if min_interval < 0 or max_interval < min_interval:
            raise ValueError("request interval must satisfy 0 <= min <= max")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        try:
            normalized_auth_ttl = float(auth_validation_ttl)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "auth_validation_ttl must be a non-negative finite number"
            ) from exc
        if not math.isfinite(normalized_auth_ttl) or normalized_auth_ttl < 0:
            raise ValueError(
                "auth_validation_ttl must be a non-negative finite number"
            )

        normalized_cookie = cookie.strip() if cookie else None
        if normalized_cookie and ("\r" in normalized_cookie or "\n" in normalized_cookie):
            raise ValueError("Cookie must not contain line breaks")

        self.cookie = normalized_cookie
        self.timeout = float(timeout)
        self.min_interval = float(min_interval)
        self.max_interval = float(max_interval)
        self.max_attempts = int(max_attempts)
        self.auth_validation_ttl = normalized_auth_ttl
        self._opener = opener or build_opener(_SameOriginHTTPSRedirectHandler())
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._last_request_at: float | None = None
        self._wbi_mixin_key: str | None = None
        self._wbi_key_fetched_at: float | None = None
        self._wbi_cookie_context: str | None = None
        self._auth_validated_at: float | None = None
        self._auth_cookie_context: str | None = None

    def resolve_video(self, value: str) -> VideoInfo:
        """Resolve a BVID or standard URL to API identifiers and owner info."""

        bvid = extract_bvid(value)
        if self.cookie:
            self._ensure_authentication_fresh()
            self._get_wbi_mixin_key()
        payload = self._request_json(
            _VIEW_URL,
            {"bvid": bvid},
            referer=f"https://www.bilibili.com/video/{bvid}/",
        )
        data = self._require_data_mapping(payload, _VIEW_URL)

        aid = _required_positive_int(data.get("aid"), "data.aid", _VIEW_URL)
        response_bvid = _optional_text(data.get("bvid")) or bvid
        if not _BVID_RE.fullmatch(response_bvid):
            raise ResponseFormatError("data.bvid is missing or invalid", _VIEW_URL)

        owner_raw = data.get("owner")
        owner = _map_author(owner_raw if isinstance(owner_raw, Mapping) else {})
        stat_raw = data.get("stat")
        reply_count: int | None = None
        if isinstance(stat_raw, Mapping):
            reply_count = _optional_nonnegative_int(stat_raw.get("reply"))

        return VideoInfo(
            aid=aid,
            bvid="BV" + response_bvid[2:],
            title=_optional_text(data.get("title")),
            owner=owner,
            reply_count=reply_count,
        )

    def fetch_root_page(
        self,
        video: VideoInfo,
        cursor: object | None = None,
    ) -> CommentPage:
        """Fetch one WBI-signed root-comment page using ``next_offset``."""

        self._ensure_authentication_fresh()
        offset: object = "" if cursor is None else cursor
        try:
            pagination = json.dumps(
                {"offset": offset},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("root comment cursor must be JSON-serializable") from exc

        params: dict[str, object] = {
            "oid": video.aid,
            "type": _VIDEO_TYPE,
            "mode": _ROOT_PAGE_MODE,
            "pagination_str": pagination,
            "plat": 1,
            "seek_rpid": "",
            "web_location": 1315875,
        }
        payload = self._request_json(
            _ROOT_REPLY_URL,
            sign_wbi(params, self._get_wbi_mixin_key()),
            referer=video.canonical_url,
        )
        data = self._require_data_mapping(payload, _ROOT_REPLY_URL)

        raw_replies: list[object] = []
        if cursor in (None, ""):
            top_replies = data.get("top_replies")
            if isinstance(top_replies, Sequence) and not isinstance(
                top_replies, (str, bytes, bytearray)
            ):
                raw_replies.extend(top_replies)

        replies = data.get("replies")
        if replies is not None:
            if not isinstance(replies, Sequence) or isinstance(
                replies, (str, bytes, bytearray)
            ):
                raise ResponseFormatError("data.replies is not a list", _ROOT_REPLY_URL)
            raw_replies.extend(replies)
        items = _map_reply_list(raw_replies, _ROOT_REPLY_URL)

        cursor_raw = data.get("cursor")
        if not isinstance(cursor_raw, Mapping):
            raise ResponseFormatError("data.cursor is missing", _ROOT_REPLY_URL)

        pagination_raw = cursor_raw.get("pagination_reply")
        next_offset: object | None = None
        if isinstance(pagination_raw, Mapping):
            next_offset = pagination_raw.get("next_offset")
        is_end = _required_bool(
            cursor_raw.get("is_end"),
            "data.cursor.is_end",
            _ROOT_REPLY_URL,
        )
        has_more = not is_end and next_offset not in (None, "")
        if not is_end and next_offset in (None, ""):
            raise ResponseFormatError(
                "root cursor is not at the end but has no next_offset",
                _ROOT_REPLY_URL,
            )
        if has_more and next_offset == cursor:
            raise ResponseFormatError(
                "root cursor repeated the current next_offset",
                _ROOT_REPLY_URL,
            )

        return CommentPage(
            items=items,
            next_cursor=next_offset if has_more else None,
            has_more=has_more,
        )

    def fetch_child_page(
        self,
        video: VideoInfo,
        root_id: str | int,
        page_no: int,
    ) -> CommentPage:
        """Fetch one page of replies below a root comment."""

        normalized_root = str(root_id).strip()
        if not normalized_root.isdigit() or int(normalized_root) <= 0:
            raise ValueError("root_id must be a positive numeric comment ID")
        if isinstance(page_no, bool) or not isinstance(page_no, int) or page_no < 1:
            raise ValueError("page_no must be a positive integer")

        self._ensure_authentication_fresh()
        payload = self._request_json(
            _CHILD_REPLY_URL,
            {
                "oid": video.aid,
                "type": _VIDEO_TYPE,
                "root": normalized_root,
                "pn": page_no,
                "ps": _CHILD_PAGE_SIZE,
            },
            referer=video.canonical_url,
        )
        data = self._require_data_mapping(payload, _CHILD_REPLY_URL)
        raw_replies = data.get("replies")
        if raw_replies is None:
            raw_replies = []
        if not isinstance(raw_replies, Sequence) or isinstance(
            raw_replies, (str, bytes, bytearray)
        ):
            raise ResponseFormatError("data.replies is not a list", _CHILD_REPLY_URL)
        items = _map_reply_list(raw_replies, _CHILD_REPLY_URL)

        page_raw = data.get("page")
        if not isinstance(page_raw, Mapping):
            raise ResponseFormatError("data.page is missing", _CHILD_REPLY_URL)
        current_page = _optional_nonnegative_int(page_raw.get("num"))
        page_size = _optional_nonnegative_int(page_raw.get("size"))
        total = _optional_nonnegative_int(page_raw.get("count"))
        if current_page is None or page_size is None or total is None:
            raise ResponseFormatError(
                "data.page num/size/count is invalid",
                _CHILD_REPLY_URL,
            )

        if current_page != page_no:
            raise ResponseFormatError(
                "data.page.num does not match the requested child page",
                _CHILD_REPLY_URL,
            )
        if page_size == 0 and total > 0:
            raise ResponseFormatError(
                "data.page.size is zero while child replies remain",
                _CHILD_REPLY_URL,
            )

        consumed = page_no * page_size
        has_more = page_size > 0 and consumed < total
        return CommentPage(
            items=items,
            next_cursor=page_no + 1 if has_more else None,
            has_more=has_more,
        )

    # SDD terminology retained as aliases for callers created before the final
    # Phase 1 method names were settled.
    def list_root_comments(
        self,
        video: VideoInfo,
        cursor: object | None = None,
    ) -> CommentPage:
        return self.fetch_root_page(video, cursor)

    def list_child_comments(
        self,
        video: VideoInfo,
        root_id: str | int,
        page_no: int,
    ) -> CommentPage:
        return self.fetch_child_page(video, root_id, page_no)

    def validate_authentication(self) -> bool:
        """Validate the configured Cookie and cache nav's WBI key.

        Anonymous clients return ``False``. If a Cookie was supplied but nav
        does not explicitly confirm a logged-in session, fail instead of
        silently continuing with anonymous comment visibility.
        """

        # Clear the old proof before going to the network. If this explicit
        # validation fails, a caller that catches the exception must not be
        # able to reuse a still-within-TTL success from an earlier request.
        self._auth_validated_at = None
        self._auth_cookie_context = None

        payload = self._request_json(
            _NAV_URL,
            referer="https://www.bilibili.com/",
            allowed_api_codes=frozenset({-101}),
        )
        data = self._require_data_mapping(payload, _NAV_URL)
        is_login = _required_bool(data.get("isLogin"), "data.isLogin", _NAV_URL)

        raw_code = payload.get("code")
        try:
            code = int(raw_code)
        except (TypeError, ValueError) as exc:
            raise ResponseFormatError("API response code is missing", _NAV_URL) from exc
        if self.cookie and (code != 0 or not is_login):
            raise AuthenticationRequiredError(
                -101,
                "configured Cookie is invalid or expired",
                _NAV_URL,
            )

        wbi_img = data.get("wbi_img")
        if not isinstance(wbi_img, Mapping):
            raise ResponseFormatError("data.wbi_img is missing", _NAV_URL)
        img_key = _wbi_filename_key(wbi_img.get("img_url"), "img_url")
        sub_key = _wbi_filename_key(wbi_img.get("sub_url"), "sub_url")
        self._wbi_mixin_key = make_mixin_key(img_key, sub_key)
        self._wbi_key_fetched_at = self._clock()
        self._wbi_cookie_context = self.cookie
        self._auth_validated_at = self._wbi_key_fetched_at
        self._auth_cookie_context = self.cookie
        return is_login

    def _ensure_authentication_fresh(self) -> None:
        """Refresh login proof when a configured Cookie's auth TTL expires."""

        if not self.cookie:
            return
        now = self._clock()
        if (
            self._auth_validated_at is not None
            and self._auth_cookie_context == self.cookie
        ):
            age = now - self._auth_validated_at
            if 0 <= age < self.auth_validation_ttl:
                return
        self.validate_authentication()

    def _get_wbi_mixin_key(self) -> str:
        now = self._clock()
        if (
            self._wbi_mixin_key is not None
            and self._wbi_key_fetched_at is not None
            and self._wbi_cookie_context == self.cookie
            and now - self._wbi_key_fetched_at < _WBI_KEY_TTL_SECONDS
        ):
            return self._wbi_mixin_key

        self.validate_authentication()
        if self._wbi_mixin_key is None:
            raise ResponseFormatError("WBI mixin key was not cached", _NAV_URL)
        return self._wbi_mixin_key

    def _request_json(
        self,
        endpoint: str,
        params: Mapping[str, object] | None = None,
        *,
        referer: str,
        allowed_api_codes: frozenset[int] = frozenset(),
    ) -> Mapping[str, Any]:
        query = urlencode(
            [(str(key), str(value)) for key, value in (params or {}).items()]
        )
        url = f"{endpoint}?{query}" if query else endpoint
        request = Request(
            url,
            headers=self._headers(referer),
            method="GET",
        )

        last_status: int | None = None
        last_reason = "temporary network request failed"
        for attempt in range(1, self.max_attempts + 1):
            self._throttle()
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    raw = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                try:
                    decoded = raw.decode(charset)
                    payload = json.loads(decoded)
                except (LookupError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ResponseFormatError(
                        "API response is not valid JSON",
                        endpoint,
                    ) from exc
                if not isinstance(payload, Mapping):
                    raise ResponseFormatError(
                        "API response root is not an object",
                        endpoint,
                    )
                self._raise_for_api_code(
                    payload,
                    endpoint,
                    allowed_codes=allowed_api_codes,
                )
                return payload
            except HTTPError as exc:
                last_status = exc.code
                last_reason = str(exc.reason or "HTTP request failed")
                if exc.code == 412:
                    raise RiskControlError(
                        -412,
                        "request blocked by Bilibili risk control",
                        endpoint,
                    ) from exc
                if exc.code in {401, 407}:
                    raise AuthenticationRequiredError(
                        exc.code,
                        "authentication required",
                        endpoint,
                    ) from exc
                if exc.code == 403:
                    raise AccessDeniedError(
                        exc.code,
                        "access denied",
                        endpoint,
                    ) from exc
                if exc.code == 404:
                    raise VideoUnavailableError(
                        exc.code,
                        "resource not found",
                        endpoint,
                    ) from exc
                if exc.code not in {408, 429} and not 500 <= exc.code < 600:
                    raise BilibiliAPIError(
                        exc.code,
                        last_reason,
                        endpoint,
                    ) from exc
                if attempt >= self.max_attempts:
                    break
                retry_after = (
                    exc.headers.get("Retry-After")
                    if exc.headers is not None
                    else None
                )
                self._sleep(self._retry_delay(attempt, retry_after))
            except (
                URLError,
                TimeoutError,
                socket.timeout,
                ConnectionError,
                OSError,
                HTTPException,
            ) as exc:
                last_reason = _transport_reason(exc)
                if attempt >= self.max_attempts:
                    break
                self._sleep(self._retry_delay(attempt))

        raise TemporaryNetworkError(
            last_reason,
            endpoint=endpoint,
            status=last_status,
            attempts=self.max_attempts,
        )

    def _headers(self, referer: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": referer,
            "Origin": "https://www.bilibili.com",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/138.0.0.0 Safari/537.36"
            ),
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _throttle(self) -> None:
        now = self._clock()
        if self._last_request_at is not None:
            target_interval = self._rng.uniform(
                self.min_interval,
                self.max_interval,
            )
            remaining = target_interval - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last_request_at = now

    def _retry_delay(self, attempt: int, retry_after: str | None = None) -> float:
        parsed = _parse_retry_after(retry_after)
        if parsed is not None:
            return min(max(parsed, 0.0), 120.0)
        return min(2 ** (attempt - 1), 30) + self._rng.uniform(0.0, 0.5)

    @staticmethod
    def _raise_for_api_code(
        payload: Mapping[str, Any],
        endpoint: str,
        *,
        allowed_codes: frozenset[int] = frozenset(),
    ) -> None:
        raw_code = payload.get("code")
        try:
            code = int(raw_code)
        except (TypeError, ValueError) as exc:
            raise ResponseFormatError("API response code is missing", endpoint) from exc
        if code == 0 or code in allowed_codes:
            return

        message = _optional_text(payload.get("message") or payload.get("msg"))
        error_type: type[BilibiliAPIError]
        if code in _AUTH_CODES:
            error_type = AuthenticationRequiredError
        elif code in _RISK_CODES:
            error_type = RiskControlError
        elif code in _ACCESS_CODES:
            error_type = AccessDeniedError
        elif code in _UNAVAILABLE_CODES:
            error_type = VideoUnavailableError
        else:
            error_type = BilibiliAPIError
        raise error_type(code, message, endpoint)

    @staticmethod
    def _require_data_mapping(
        payload: Mapping[str, Any],
        endpoint: str,
    ) -> Mapping[str, Any]:
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise ResponseFormatError("API response data is missing", endpoint)
        return data


def _map_reply_list(raw_replies: Sequence[object], endpoint: str) -> list[Comment]:
    items: list[Comment] = []
    seen: set[str] = set()
    for raw in raw_replies:
        if raw is None:
            continue
        if not isinstance(raw, Mapping):
            raise ResponseFormatError("comment entry is not an object", endpoint)
        comment = _map_comment(raw, endpoint)
        if comment.rpid not in seen:
            seen.add(comment.rpid)
            items.append(comment)
    return items


def _map_comment(raw: Mapping[str, object], endpoint: str) -> Comment:
    rpid = _required_identifier(raw.get("rpid"), "comment.rpid", endpoint)
    raw_root = _optional_identifier(raw.get("root"))
    raw_parent = _optional_identifier(raw.get("parent"))
    root = raw_root or rpid

    member_raw = raw.get("member")
    author = _map_author(member_raw if isinstance(member_raw, Mapping) else {})
    content_raw = raw.get("content")
    content = ""
    if isinstance(content_raw, Mapping):
        content = _optional_text(content_raw.get("message"))

    control_raw = raw.get("reply_control")
    location: str | None = None
    if isinstance(control_raw, Mapping):
        location = _optional_text(control_raw.get("location")) or None

    reply_to_author = _map_parent_reply_author(raw.get("parent_reply_member"))

    ctime = _optional_nonnegative_int(raw.get("ctime"))
    if ctime is None:
        raise ResponseFormatError("comment.ctime is missing or invalid", endpoint)

    return Comment(
        rpid=rpid,
        root=root,
        parent=raw_parent,
        author=author,
        content=content,
        ctime=ctime,
        likes=_optional_nonnegative_int(raw.get("like")),
        location=location,
        rcount=_optional_nonnegative_int(raw.get("rcount")) or 0,
        reply_to_author=reply_to_author,
    )


def _map_author(raw: Mapping[str, object]) -> Author:
    return Author(
        mid=_optional_identifier(raw.get("mid")) or "",
        name=_optional_text(raw.get("uname")),
    )


def _map_parent_reply_author(raw: object) -> Author | None:
    """Map Bilibili's explicit direct-parent member without inferring mentions."""

    if not isinstance(raw, Mapping):
        return None
    mid = _optional_identifier(raw.get("mid"))
    if mid is None:
        return None
    return Author(
        mid=mid,
        name=_optional_text(raw.get("name")) or _optional_text(raw.get("uname")),
    )


def _wbi_filename_key(value: object, field: str) -> str:
    text = _optional_text(value)
    path = urlsplit(text).path
    filename = path.rsplit("/", 1)[-1]
    key = filename.split(".", 1)[0]
    if not key:
        raise ResponseFormatError(f"data.wbi_img.{field} is invalid", _NAV_URL)
    return key


def _optional_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _optional_identifier(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized or normalized == "0":
            return None
        return normalized
    return None


def _required_identifier(value: object, field: str, endpoint: str) -> str:
    normalized = _optional_identifier(value)
    if normalized is None:
        raise ResponseFormatError(f"{field} is missing or invalid", endpoint)
    return normalized


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _required_positive_int(value: object, field: str, endpoint: str) -> int:
    result = _optional_nonnegative_int(value)
    if result is None or result <= 0:
        raise ResponseFormatError(f"{field} is missing or invalid", endpoint)
    return result


def _required_bool(value: object, field: str, endpoint: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str) and value.lower() in {"0", "1", "false", "true"}:
        return value.lower() in {"1", "true"}
    raise ResponseFormatError(f"{field} is missing or invalid", endpoint)


def _url_origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    if scheme not in {"http", "https"} or not host:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, host, port


def _transport_reason(error: BaseException) -> str:
    if isinstance(error, URLError):
        reason = error.reason
        return str(reason) if reason else "URL request failed"
    return str(error) or type(error).__name__


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return (retry_at - datetime.now(timezone.utc)).total_seconds()


__all__ = [
    "AccessDeniedError",
    "AuthenticationRequiredError",
    "BilibiliAPIError",
    "BilibiliClient",
    "BilibiliError",
    "InvalidVideoInput",
    "ResponseFormatError",
    "RiskControlError",
    "TemporaryNetworkError",
    "VideoUnavailableError",
    "extract_bvid",
    "make_mixin_key",
    "sign_wbi",
]

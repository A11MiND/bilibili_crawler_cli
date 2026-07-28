"""Durable CSV output and per-video crawl checkpoints.

The CSV is the source of truth for de-duplication.  A checkpoint only advances
after the caller has appended a batch and copied ``CsvStore.committed_bytes``
into the checkpoint.
"""

from __future__ import annotations

import codecs
import csv
import errno
import io
import json
import os
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Phase 1 targets Unix-like terminals.
    fcntl = None


CSV_COLUMNS: tuple[str, ...] = (
    "一级评论序号",
    "隶属关系",
    "评论ID",
    "根评论ID",
    "父评论ID",
    "被评论者昵称",
    "被评论者ID",
    "评论者昵称",
    "评论者用户ID",
    "评论内容",
    "发布时间",
    "点赞数",
    "IP属地",
)
CSV_FIELDNAMES = CSV_COLUMNS


class StorageError(RuntimeError):
    """Base error for local output or checkpoint corruption."""


class CsvStorageError(StorageError):
    """The CSV cannot be safely read, reconciled, or written."""


class CheckpointError(StorageError):
    """The checkpoint is missing required data or cannot be written."""


def _open_private_regular_fd(
    path: Path,
    flags: int,
    mode: int = 0o600,
) -> int:
    """Open one user-owned regular file without following its final symlink."""

    nofollow = getattr(os, "O_NOFOLLOW", None)
    geteuid = getattr(os, "geteuid", None)
    if nofollow is None or geteuid is None:
        raise OSError(
            errno.ENOTSUP,
            "secure no-follow file opening is unavailable",
            os.fspath(path),
        )

    descriptor = os.open(
        path,
        flags
        | nofollow
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0),
        mode,
    )
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != geteuid()
        ):
            raise OSError(
                errno.EPERM,
                "file must be a user-owned regular file with one link",
                os.fspath(path),
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class TaskLock:
    """Hold a non-blocking, process-scoped exclusive lock for one crawl.

    The lock file is deliberately kept after release: unlinking an advisory
    lock file can create two independently locked inodes while another process
    is waiting.  Kernel advisory locks are released automatically when the
    process exits, including abnormal termination.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def __enter__(self) -> TaskLock:
        if fcntl is None:
            raise StorageError(
                "this platform does not provide the advisory file locking "
                "required for safe crawling"
            )

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = _open_private_regular_fd(
                self.path,
                os.O_CREAT | os.O_RDWR,
            )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(descriptor)
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise StorageError(
                        f"task is already running for lock: {self.path}"
                    ) from exc
                raise

            self._fd = descriptor
            try:
                os.fchmod(descriptor, 0o600)
                os.ftruncate(descriptor, 0)
                os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
                os.fsync(descriptor)
            except OSError:
                self.__exit__()
                raise
            return self
        except StorageError:
            raise
        except OSError as exc:
            raise StorageError(f"failed to acquire task lock: {self.path}") from exc

    def __exit__(self, *_: object) -> None:
        descriptor = self._fd
        self._fd = None
        if descriptor is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@dataclass(frozen=True, slots=True)
class CsvRow:
    """One row in the PRD CSV contract."""

    root_sequence: int
    relation: str
    comment_id: str
    root_id: str
    parent_id: str | None
    replied_to_name: str | None
    replied_to_id: str | None
    author_name: str
    author_id: str
    content: str
    published_at: str
    like_count: int | None
    ip_location: str | None

    def as_csv_dict(self) -> dict[str, object]:
        return {
            "一级评论序号": self.root_sequence,
            "隶属关系": self.relation,
            "评论ID": self.comment_id,
            "根评论ID": self.root_id,
            "父评论ID": self.parent_id,
            "被评论者昵称": self.replied_to_name,
            "被评论者ID": self.replied_to_id,
            "评论者昵称": self.author_name,
            "评论者用户ID": self.author_id,
            "评论内容": self.content,
            "发布时间": self.published_at,
            "点赞数": self.like_count,
            "IP属地": self.ip_location,
        }


def _header_bytes() -> bytes:
    stream = io.StringIO(newline="")
    csv.writer(stream).writerow(CSV_COLUMNS)
    return codecs.BOM_UTF8 + stream.getvalue().encode("utf-8")


_CSV_HEADER_BYTES = _header_bytes()
_EXCEL_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n")
_EXCEL_TEXT_COLUMNS = frozenset(
    {
        "评论ID",
        "根评论ID",
        "父评论ID",
        "被评论者昵称",
        "被评论者ID",
        "评论者昵称",
        "评论者用户ID",
        "评论内容",
        "发布时间",
        "IP属地",
    }
)
AUTH_MODES = frozenset({"anonymous", "authenticated"})
CHILD_STRATEGIES = frozenset({"page", "detail"})


def escape_excel_text(value: object) -> str:
    """Return a reversible, Excel-safe representation of user-controlled text.

    Excel can interpret a CSV cell beginning with ``=``, ``+``, ``-``, ``@``,
    tab, or carriage return as a formula.  Such values receive one leading
    apostrophe.  Values that already begin with an apostrophe also receive one,
    so :func:`unescape_excel_text` can always remove exactly the marker added by
    this function without losing an original apostrophe.
    """

    text = _text(value)
    if text.startswith("'") or text.startswith(_EXCEL_FORMULA_PREFIXES):
        return "'" + text
    return text


def unescape_excel_text(value: object) -> str:
    """Reverse :func:`escape_excel_text` for a value read from this CSV."""

    text = _text(value)
    if text.startswith("''"):
        return text[1:]
    if len(text) >= 2 and text[0] == "'" and text[1] in _EXCEL_FORMULA_PREFIXES:
        return text[1:]
    return text


class CsvStore:
    """Append-only CSV store with durable batches and ID de-duplication."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        committed_bytes: int | None = None,
        expected_rows: int | None = None,
        *,
        create: bool = True,
    ) -> None:
        self.path = Path(path)
        self.seen_ids: set[str] = set()
        self.root_sequences: dict[str, int] = {}
        self.authors: dict[str, tuple[str, str]] = {}
        self.ip_location_count = 0
        self.committed_bytes = 0
        self._root_by_sequence: dict[int, str] = {}
        self._healthy = True

        if not self.path.exists():
            if not create:
                raise CsvStorageError(f"CSV is missing: {self.path}")
            if committed_bytes is not None:
                raise CsvStorageError(
                    f"CSV is missing but checkpoint expects {committed_bytes} bytes: "
                    f"{self.path}"
                )
            self._create()

        if committed_bytes is None:
            self.committed_bytes = self._load_indexes()
            self._validate_expected_rows(expected_rows)
        else:
            self.recover_to_committed_bytes(
                committed_bytes,
                expected_rows=expected_rows,
            )

    @property
    def rows_written(self) -> int:
        """Number of unique comment IDs already present in the CSV."""

        return len(self.seen_ids)

    @property
    def max_root_sequence(self) -> int:
        return max(self._root_by_sequence, default=0)

    @property
    def is_header_only(self) -> bool:
        """Whether the file is exactly the canonical BOM-prefixed header."""

        try:
            descriptor = _open_private_regular_fd(self.path, os.O_RDONLY)
            with os.fdopen(descriptor, "rb") as source:
                data = source.read()
            return not self.seen_ids and data == _CSV_HEADER_BYTES
        except OSError as exc:
            raise CsvStorageError(f"failed to inspect CSV: {self.path}") from exc

    def author_for(self, comment_id: str) -> tuple[str, str] | None:
        return self.authors.get(str(comment_id))

    def root_sequence_for(self, root_id: str) -> int | None:
        return self.root_sequences.get(str(root_id))

    def append_rows(
        self,
        rows: Iterable[CsvRow | Mapping[str, object]],
    ) -> int:
        """Append a durable batch and return the number of new rows.

        IDs already present in the file and repeated IDs within this batch are
        skipped.  Indexes are only advanced after ``flush`` and ``fsync`` both
        succeed.
        """

        if not self._healthy:
            raise CsvStorageError(
                "CSV store is not writable after an earlier failed append; "
                "reopen it with the last checkpoint"
            )

        batch_ids: set[str] = set()
        new_rows: list[dict[str, str | int]] = []
        new_root_sequences: dict[str, int] = {}
        new_root_by_sequence: dict[int, str] = {}

        for row in rows:
            normalised = self._normalise_row(row)
            comment_id = str(normalised["评论ID"])
            if comment_id in self.seen_ids or comment_id in batch_ids:
                continue

            self._validate_root_sequence(
                normalised,
                new_root_sequences,
                new_root_by_sequence,
            )
            self._validate_child_root(
                normalised,
                new_root_sequences,
            )
            batch_ids.add(comment_id)
            new_rows.append(normalised)

        if not new_rows:
            return 0

        try:
            descriptor = _open_private_regular_fd(
                self.path,
                os.O_WRONLY | os.O_APPEND,
            )
            with os.fdopen(
                descriptor,
                "a",
                encoding="utf-8",
                newline="",
            ) as output:
                writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
                writer.writerows(
                    self._serialise_row(row) for row in new_rows
                )
                output.flush()
                os.fsync(output.fileno())
                new_size = os.fstat(output.fileno()).st_size
        except (OSError, UnicodeError, csv.Error) as exc:
            self._healthy = False
            raise CsvStorageError(f"failed to append CSV batch: {self.path}") from exc

        self.committed_bytes = new_size
        for row in new_rows:
            self._index_row(row)
        return len(new_rows)

    def recover_to_committed_bytes(
        self,
        committed_bytes: int,
        *,
        expected_rows: int | None = None,
    ) -> None:
        """Reconcile the CSV to the byte length in a durable checkpoint.

        An uncommitted tail is truncated and synced.  A file shorter than the
        checkpoint is treated as external data loss and is never extended.
        Before any truncation, the committed prefix is read and validated as a
        complete strict CSV whose row count matches ``expected_rows`` when
        provided.  Validation failure therefore never mutates the file.
        """

        target = _non_negative_int(
            committed_bytes,
            "committed_bytes",
            CsvStorageError,
        )
        if target < len(_CSV_HEADER_BYTES):
            raise CsvStorageError(
                "checkpoint committed_bytes is smaller than the CSV header"
            )
        if not self.path.exists():
            raise CsvStorageError(f"CSV is missing: {self.path}")

        try:
            descriptor = _open_private_regular_fd(self.path, os.O_RDWR)
            with os.fdopen(descriptor, "r+b") as output:
                actual = os.fstat(output.fileno()).st_size
                if actual < target:
                    raise CsvStorageError(
                        f"CSV is shorter than checkpoint: actual={actual}, "
                        f"committed={target}"
                    )
                output.seek(0)
                committed_prefix = output.read(target)
                if len(committed_prefix) != target:
                    raise CsvStorageError(
                        "failed to read the complete committed CSV prefix"
                    )
                self._load_indexes_from_bytes(committed_prefix)
                self._validate_expected_rows(expected_rows)
                if actual > target:
                    output.truncate(target)
                    output.flush()
                    os.fsync(output.fileno())
        except CsvStorageError:
            raise
        except OSError as exc:
            raise CsvStorageError(f"failed to reconcile CSV: {self.path}") from exc

        self._healthy = True
        self.committed_bytes = target

    def _create(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = _open_private_regular_fd(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(_CSV_HEADER_BYTES)
                output.flush()
                os.fsync(output.fileno())
            _fsync_directory(self.path.parent)
        except FileExistsError:
            return
        except OSError as exc:
            raise CsvStorageError(f"failed to create CSV: {self.path}") from exc

    def _load_indexes(self) -> int:
        try:
            descriptor = _open_private_regular_fd(self.path, os.O_RDONLY)
            with os.fdopen(descriptor, "rb") as raw:
                data = raw.read()
            self._load_indexes_from_bytes(data)
            return len(data)
        except CsvStorageError:
            raise
        except (OSError, UnicodeError, csv.Error) as exc:
            raise CsvStorageError(f"failed to read CSV: {self.path}") from exc

    def _load_indexes_from_bytes(self, data: bytes) -> None:
        try:
            self._validate_binary_csv_envelope(data)
            decoded = data.decode("utf-8-sig")
            source = io.StringIO(decoded, newline="")
            self._load_indexes_from_text(source)
        except CsvStorageError:
            raise
        except (UnicodeError, csv.Error) as exc:
            raise CsvStorageError(
                f"failed to read committed CSV prefix: {self.path}"
            ) from exc

    def _load_indexes_from_text(self, source: io.TextIOBase) -> None:
        self.seen_ids.clear()
        self.root_sequences.clear()
        self.authors.clear()
        self._root_by_sequence.clear()
        self.ip_location_count = 0

        reader = csv.DictReader(source, strict=True)
        if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
            raise CsvStorageError(
                f"CSV header does not match the data contract: {self.path}"
            )
        for line_number, row in enumerate(reader, start=2):
            try:
                if any(value is None for value in row.values()):
                    raise CsvStorageError(
                        "CSV record has missing trailing fields"
                    )
                normalised = self._normalise_row(row, from_csv=True)
            except CsvStorageError as exc:
                raise CsvStorageError(
                    f"invalid CSV row at record {line_number}: "
                    f"{self.path}: {exc}"
                ) from exc
            comment_id = str(normalised["评论ID"])
            if comment_id in self.seen_ids:
                raise CsvStorageError(
                    f"duplicate comment ID {comment_id} "
                    f"at record {line_number}: {self.path}"
                )
            self._validate_root_sequence(normalised, {}, {})
            self._validate_child_root(normalised, {})
            self._index_row(normalised)
        expected_sequences = set(range(1, len(self.root_sequences) + 1))
        if set(self._root_by_sequence) != expected_sequences:
            raise CsvStorageError(
                f"root comment sequences are not contiguous: {self.path}"
            )

    def _validate_binary_csv_envelope(self, data: bytes) -> None:
        if not data.startswith(codecs.BOM_UTF8):
            raise CsvStorageError(
                f"CSV must start with a UTF-8 BOM: {self.path}"
            )
        if not data.endswith(b"\r\n"):
            raise CsvStorageError(
                f"CSV must end at a complete CRLF record boundary: {self.path}"
            )

    def _validate_expected_rows(self, expected_rows: int | None) -> None:
        if expected_rows is None:
            return
        expected = _non_negative_int(
            expected_rows,
            "expected_rows",
            CsvStorageError,
        )
        if self.rows_written != expected:
            raise CsvStorageError(
                "CSV row count does not match checkpoint: "
                f"actual={self.rows_written}, expected={expected}"
            )

    def _normalise_row(
        self,
        row: CsvRow | Mapping[str, object],
        *,
        from_csv: bool = False,
    ) -> dict[str, str | int]:
        values: Mapping[str, object]
        if isinstance(row, CsvRow):
            values = row.as_csv_dict()
        elif isinstance(row, Mapping):
            values = row
        else:
            raise CsvStorageError(f"unsupported CSV row type: {type(row).__name__}")

        if set(values) != set(CSV_COLUMNS):
            raise CsvStorageError("CSV row fields do not match the data contract")

        sequence = _positive_int(
            values["一级评论序号"],
            "一级评论序号",
            CsvStorageError,
        )
        relation = _text(values["隶属关系"])
        if relation not in {"一级评论", "二级评论"}:
            raise CsvStorageError("隶属关系 must be 一级评论 or 二级评论")

        comment_id = _required_text(values["评论ID"], "评论ID")
        root_id = _required_text(values["根评论ID"], "根评论ID")
        parent_id = _text(values["父评论ID"])
        if relation == "一级评论":
            if root_id != comment_id:
                raise CsvStorageError("一级评论的根评论ID必须等于评论ID")
            if parent_id:
                raise CsvStorageError("一级评论的父评论ID必须为空")
        elif not parent_id:
            raise CsvStorageError("二级评论的父评论ID不能为空")

        like_value = values["点赞数"]
        if like_value in (None, ""):
            like_count: str | int = ""
        else:
            like_count = _non_negative_int(
                like_value,
                "点赞数",
                CsvStorageError,
            )

        normalised: dict[str, str | int] = {
            "一级评论序号": sequence,
            "隶属关系": relation,
            "评论ID": comment_id,
            "根评论ID": root_id,
            "父评论ID": parent_id,
            "被评论者昵称": _text(values["被评论者昵称"]),
            "被评论者ID": _text(values["被评论者ID"]),
            "评论者昵称": _text(values["评论者昵称"]),
            "评论者用户ID": _text(values["评论者用户ID"]),
            "评论内容": _text(values["评论内容"]),
            "发布时间": _text(values["发布时间"]),
            "点赞数": like_count,
            "IP属地": _text(values["IP属地"]),
        }
        if from_csv:
            for column in _EXCEL_TEXT_COLUMNS:
                physical_text = _text(normalised[column])
                if physical_text.startswith(_EXCEL_FORMULA_PREFIXES):
                    raise CsvStorageError(
                        f"{column} contains an unescaped Excel formula prefix; "
                        "use --restart to regenerate a protected CSV"
                    )
                normalised[column] = unescape_excel_text(physical_text)
        return normalised

    @staticmethod
    def _serialise_row(
        row: Mapping[str, str | int],
    ) -> dict[str, str | int]:
        serialised = dict(row)
        for column in _EXCEL_TEXT_COLUMNS:
            serialised[column] = escape_excel_text(serialised[column])
        return serialised

    def _validate_root_sequence(
        self,
        row: Mapping[str, str | int],
        pending_by_root: dict[str, int],
        pending_by_sequence: dict[int, str],
    ) -> None:
        if row["隶属关系"] != "一级评论":
            return

        root_id = str(row["根评论ID"])
        sequence = int(row["一级评论序号"])
        known_sequence = self.root_sequences.get(root_id)
        if known_sequence is not None and known_sequence != sequence:
            raise CsvStorageError(
                f"root {root_id} has conflicting root sequence values"
            )
        pending_sequence = pending_by_root.get(root_id)
        if pending_sequence is not None and pending_sequence != sequence:
            raise CsvStorageError(
                f"root {root_id} has conflicting root sequence values"
            )

        known_root = self._root_by_sequence.get(sequence)
        if known_root is not None and known_root != root_id:
            raise CsvStorageError(
                f"root sequence {sequence} is already assigned to {known_root}"
            )
        pending_root = pending_by_sequence.get(sequence)
        if pending_root is not None and pending_root != root_id:
            raise CsvStorageError(
                f"root sequence {sequence} is repeated in the batch"
            )
        if known_sequence is None and pending_sequence is None:
            expected_sequence = (
                self.max_root_sequence + len(pending_by_root) + 1
            )
            if sequence != expected_sequence:
                raise CsvStorageError(
                    "new root comments must use contiguous sequences; "
                    f"expected {expected_sequence}, got {sequence}"
                )

        pending_by_root[root_id] = sequence
        pending_by_sequence[sequence] = root_id

    def _index_row(self, row: Mapping[str, str | int]) -> None:
        comment_id = str(row["评论ID"])
        self.seen_ids.add(comment_id)
        self.authors[comment_id] = (
            str(row["评论者用户ID"]),
            str(row["评论者昵称"]),
        )
        if _text(row["IP属地"]).strip():
            self.ip_location_count += 1
        if row["隶属关系"] == "一级评论":
            root_id = str(row["根评论ID"])
            sequence = int(row["一级评论序号"])
            self.root_sequences[root_id] = sequence
            self._root_by_sequence[sequence] = root_id

    def _validate_child_root(
        self,
        row: Mapping[str, str | int],
        pending_by_root: Mapping[str, int],
    ) -> None:
        if row["隶属关系"] != "二级评论":
            return
        root_id = str(row["根评论ID"])
        sequence = self.root_sequences.get(root_id)
        if sequence is None:
            sequence = pending_by_root.get(root_id)
        if sequence is None:
            raise CsvStorageError(
                f"child comment references unknown root {root_id}"
            )
        if int(row["一级评论序号"]) != sequence:
            raise CsvStorageError(
                f"child comment has a conflicting root sequence for {root_id}"
            )


@dataclass(slots=True)
class Checkpoint:
    """Serializable crawl position for one BVID."""

    bvid: str
    aid: int
    schema_version: int = 3
    auth_mode: str | None = "anonymous"
    status: str = "running"
    phase: str = "root_page"
    main_cursor: object | None = None
    next_main_cursor: object | None = None
    completed_root_ids_in_page: list[str] = field(default_factory=list)
    current_root_id: str | None = None
    sub_cursor: object | None = None
    child_strategy: str = "page"
    next_root_sequence: int = 1
    rows_written: int = 0
    committed_bytes: int = 0
    updated_at: str = ""

    def to_dict(self) -> dict[str, object]:
        self.validate()
        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "bvid": self.bvid,
            "aid": self.aid,
            "status": self.status,
            "phase": self.phase,
            "main_cursor": self.main_cursor,
            "next_main_cursor": self.next_main_cursor,
            "completed_root_ids_in_page": list(self.completed_root_ids_in_page),
            "current_root_id": self.current_root_id,
            "sub_cursor": self.sub_cursor,
            "next_root_sequence": self.next_root_sequence,
            "rows_written": self.rows_written,
            "committed_bytes": self.committed_bytes,
            "updated_at": self.updated_at,
        }
        if self.schema_version in {2, 3}:
            value["auth_mode"] = self.auth_mode
        if self.schema_version == 3:
            value["child_strategy"] = self.child_strategy
        return value

    def validate(self) -> None:
        if type(self.schema_version) is not int or self.schema_version not in {
            1,
            2,
            3,
        }:
            raise CheckpointError(
                f"unsupported checkpoint schema_version: {self.schema_version}"
            )
        _safe_bvid(self.bvid)
        _exact_positive_int(self.aid, "aid", CheckpointError)
        if self.schema_version == 1:
            if self.auth_mode is not None:
                raise CheckpointError(
                    "schema version 1 checkpoint must not contain auth_mode"
                )
        elif self.auth_mode not in AUTH_MODES:
            raise CheckpointError(
                f"invalid checkpoint auth_mode: {self.auth_mode!r}"
            )
        if self.child_strategy not in CHILD_STRATEGIES:
            raise CheckpointError(
                f"invalid checkpoint child_strategy: {self.child_strategy!r}"
            )
        if self.schema_version in {1, 2} and self.child_strategy != "page":
            raise CheckpointError(
                f"schema version {self.schema_version} checkpoint must use "
                "child_strategy='page'"
            )
        if self.status not in {"running", "complete"}:
            raise CheckpointError(f"invalid checkpoint status: {self.status!r}")
        if self.phase not in {"root_page", "child_page", "complete"}:
            raise CheckpointError(f"invalid checkpoint phase: {self.phase!r}")
        if not isinstance(self.completed_root_ids_in_page, list) or not all(
            isinstance(value, str) and value
            for value in self.completed_root_ids_in_page
        ):
            raise CheckpointError(
                "completed_root_ids_in_page must be a list of non-empty strings"
            )
        if len(set(self.completed_root_ids_in_page)) != len(
            self.completed_root_ids_in_page
        ):
            raise CheckpointError(
                "completed_root_ids_in_page must not contain duplicates"
            )
        if self.current_root_id is not None and (
            not isinstance(self.current_root_id, str)
            or not self.current_root_id
        ):
            raise CheckpointError(
                "current_root_id must be a non-empty string or null"
            )
        if self.sub_cursor is not None:
            if self.child_strategy == "detail":
                _exact_non_negative_int(
                    self.sub_cursor,
                    "sub_cursor",
                    CheckpointError,
                )
            else:
                _exact_positive_int(
                    self.sub_cursor,
                    "sub_cursor",
                    CheckpointError,
                )
        _exact_positive_int(
            self.next_root_sequence,
            "next_root_sequence",
            CheckpointError,
        )
        _exact_non_negative_int(
            self.rows_written,
            "rows_written",
            CheckpointError,
        )
        _exact_non_negative_int(
            self.committed_bytes,
            "committed_bytes",
            CheckpointError,
        )
        if not isinstance(self.updated_at, str) or not self.updated_at:
            raise CheckpointError("updated_at must be a non-empty string")

        if self.status == "complete":
            if self.phase != "complete":
                raise CheckpointError(
                    "complete checkpoint must use phase='complete'"
                )
            if self.current_root_id is not None or self.sub_cursor is not None:
                raise CheckpointError(
                    "complete checkpoint cannot retain child pagination state"
                )
            if self.child_strategy != "page":
                raise CheckpointError(
                    "complete checkpoint must reset child_strategy='page'"
                )
            if self.next_main_cursor is not None:
                raise CheckpointError(
                    "complete checkpoint cannot retain next_main_cursor"
                )
        else:
            if self.phase == "complete":
                raise CheckpointError(
                    "running checkpoint cannot use phase='complete'"
                )
            if self.phase == "root_page":
                if self.current_root_id is not None or self.sub_cursor is not None:
                    raise CheckpointError(
                        "root_page checkpoint cannot retain child pagination state"
                    )
                if self.child_strategy != "page":
                    raise CheckpointError(
                        "root_page checkpoint must reset child_strategy='page'"
                    )
            elif self.current_root_id is None:
                raise CheckpointError(
                    "child_page checkpoint must identify current_root_id"
                )
            elif self.sub_cursor is None and self.schema_version != 1:
                raise CheckpointError(
                    f"schema version {self.schema_version} child_page checkpoint "
                    "must identify "
                    "the next child page"
                )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Checkpoint:
        common_fields = {
            "schema_version",
            "bvid",
            "aid",
            "status",
            "phase",
            "main_cursor",
            "next_main_cursor",
            "completed_root_ids_in_page",
            "current_root_id",
            "sub_cursor",
            "next_root_sequence",
            "rows_written",
            "committed_bytes",
            "updated_at",
        }
        schema_version = value.get("schema_version")
        if schema_version == 1:
            expected = common_fields
            auth_mode: str | None = None
            child_strategy = "page"
        elif schema_version == 2:
            expected = common_fields | {"auth_mode"}
            auth_mode = value.get("auth_mode")
            child_strategy = "page"
        elif schema_version == 3:
            expected = common_fields | {"auth_mode", "child_strategy"}
            auth_mode = value.get("auth_mode")
            child_strategy = value.get("child_strategy")
        else:
            raise CheckpointError(
                f"unsupported checkpoint schema_version: {schema_version}"
            )
        if set(value) != expected:
            raise CheckpointError(
                "checkpoint fields do not match "
                f"schema version {schema_version}"
            )
        checkpoint = cls(
            schema_version=schema_version,
            auth_mode=auth_mode,
            child_strategy=child_strategy,
            bvid=value["bvid"],
            aid=value["aid"],
            status=value["status"],
            phase=value["phase"],
            main_cursor=value["main_cursor"],
            next_main_cursor=value["next_main_cursor"],
            completed_root_ids_in_page=value["completed_root_ids_in_page"],
            current_root_id=value["current_root_id"],
            sub_cursor=value["sub_cursor"],
            next_root_sequence=value["next_root_sequence"],
            rows_written=value["rows_written"],
            committed_bytes=value["committed_bytes"],
            updated_at=value["updated_at"],
        )
        checkpoint.validate()
        return checkpoint


class CheckpointStore:
    """Atomically persist one ``state/{BVID}.json`` checkpoint."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str],
        bvid: str,
    ) -> None:
        self.bvid = _safe_bvid(bvid)
        self.path = Path(state_dir) / f"{self.bvid}.json"

    def load(self) -> Checkpoint | None:
        if not self.path.exists():
            return None
        try:
            descriptor = _open_private_regular_fd(self.path, os.O_RDONLY)
            with os.fdopen(descriptor, "r", encoding="utf-8") as source:
                value = json.load(source)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CheckpointError(
                f"failed to read checkpoint: {self.path}"
            ) from exc
        if not isinstance(value, dict):
            raise CheckpointError("checkpoint root must be a JSON object")
        checkpoint = Checkpoint.from_dict(value)
        if checkpoint.bvid != self.bvid:
            raise CheckpointError(
                f"checkpoint BVID mismatch: expected {self.bvid}, "
                f"got {checkpoint.bvid}"
            )
        return checkpoint

    def create(
        self,
        aid: int,
        committed_bytes: int,
        auth_mode: str = "anonymous",
    ) -> Checkpoint:
        if self.path.exists():
            raise CheckpointError(f"checkpoint already exists: {self.path}")
        if auth_mode not in AUTH_MODES:
            raise CheckpointError(f"invalid checkpoint auth_mode: {auth_mode!r}")
        checkpoint = Checkpoint(
            bvid=self.bvid,
            aid=_exact_positive_int(aid, "aid", CheckpointError),
            auth_mode=auth_mode,
            committed_bytes=_exact_non_negative_int(
                committed_bytes,
                "committed_bytes",
                CheckpointError,
            ),
        )
        self.save(checkpoint)
        return checkpoint

    def save(self, checkpoint: Checkpoint) -> None:
        if checkpoint.bvid != self.bvid:
            raise CheckpointError(
                f"refusing to save checkpoint for {checkpoint.bvid} "
                f"as {self.bvid}"
            )
        if checkpoint.schema_version == 1:
            raise CheckpointError(
                "schema version 1 checkpoints are read-only; migrate to "
                "schema version 3 before saving"
            )
        checkpoint.updated_at = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        try:
            payload = (
                json.dumps(
                    checkpoint.to_dict(),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CheckpointError("checkpoint contains non-JSON data") from exc

        temporary: Path | None = None
        descriptor: int | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            temporary = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as output:
                descriptor = None
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            _fsync_directory(self.path.parent)
        except OSError as exc:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            try:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise CheckpointError(
                f"failed to save checkpoint: {self.path}"
            ) from exc


def backup_for_restart(
    *paths: str | os.PathLike[str],
    timestamp: str | None = None,
) -> tuple[Path, ...]:
    """Rename existing task files to ``.bak.{timestamp}``.

    All destinations are checked before the first rename.  If a later rename
    fails, already moved files are rolled back where possible.
    """

    stamp = timestamp or datetime.now().astimezone().strftime(
        "%Y%m%dT%H%M%S%f%z"
    )
    if not stamp or Path(stamp).name != stamp:
        raise StorageError("backup timestamp must be a single path-safe name")

    sources: list[Path] = []
    seen_paths: set[Path] = set()
    for raw_path in paths:
        source = Path(raw_path)
        if source in seen_paths or not source.exists():
            continue
        seen_paths.add(source)
        sources.append(source)

    destinations = [
        source.with_name(f"{source.name}.bak.{stamp}") for source in sources
    ]
    for destination in destinations:
        if destination.exists():
            raise StorageError(f"backup already exists: {destination}")

    moved: list[tuple[Path, Path]] = []
    directories = sorted(
        {
            path.parent
            for pair in zip(sources, destinations, strict=True)
            for path in pair
        },
        key=os.fspath,
    )
    try:
        for source, destination in zip(sources, destinations, strict=True):
            os.replace(source, destination)
            moved.append((source, destination))
        for directory in directories:
            _fsync_directory(directory)
    except OSError as exc:
        for source, destination in reversed(moved):
            try:
                os.replace(destination, source)
            except OSError:
                pass
        for directory in directories:
            try:
                _fsync_directory(directory)
            except OSError:
                pass
        raise StorageError("failed to back up files for restart") from exc
    return tuple(destinations)


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry changes after an atomic file replacement."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _safe_bvid(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise CheckpointError("bvid must be a non-empty string")
    if Path(value).name != value or value in {".", ".."}:
        raise CheckpointError("bvid must not contain path separators")
    return value


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _required_text(value: object, name: str) -> str:
    result = _text(value)
    if not result:
        raise CsvStorageError(f"{name} cannot be empty")
    return result


def _non_negative_int(
    value: object,
    name: str,
    error_type: type[StorageError] = StorageError,
) -> int:
    if isinstance(value, bool):
        raise error_type(f"{name} must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise error_type(f"{name} must be a non-negative integer") from exc
    if result < 0 or str(value).strip() != str(result):
        raise error_type(f"{name} must be a non-negative integer")
    return result


def _positive_int(
    value: object,
    name: str,
    error_type: type[StorageError] = StorageError,
) -> int:
    result = _non_negative_int(value, name, error_type)
    if result < 1:
        raise error_type(f"{name} must be a positive integer")
    return result


def _exact_non_negative_int(
    value: object,
    name: str,
    error_type: type[StorageError] = StorageError,
) -> int:
    if type(value) is not int or value < 0:
        raise error_type(f"{name} must be a non-negative JSON integer")
    return value


def _exact_positive_int(
    value: object,
    name: str,
    error_type: type[StorageError] = StorageError,
) -> int:
    result = _exact_non_negative_int(value, name, error_type)
    if result < 1:
        raise error_type(f"{name} must be a positive JSON integer")
    return result

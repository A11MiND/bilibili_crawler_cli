#!/usr/bin/env python3
"""Audit built Python distributions for private or unsafe content.

The audit reads archives in place and never extracts them. It intentionally
uses only the Python standard library so it can run before a release artifact
is trusted.
"""

from __future__ import annotations

import argparse
import re
import stat
import sys
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath


MAX_MEMBER_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024

FORBIDDEN_DIRECTORIES = {
    ".agents",
    ".codex",
    ".git",
    ".github",
    "__pycache__",
    "internal",
    "output",
    "private",
    "state",
    "test",
    "tests",
}
FORBIDDEN_DOCUMENTS = {
    "agents.md",
    "devplan.md",
    "prd.md",
    "requirement.md",
    "sdd.md",
}
FORBIDDEN_FILES = {
    ".env",
    ".netrc",
    ".pypirc",
    ".ds_store",
    "cookie.txt",
    "cookies.txt",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
}
FORBIDDEN_SUFFIXES = {
    ".key",
    ".pem",
    ".pyc",
    ".pyo",
}
CORE_PACKAGE_FILES = {
    "bili_comments/__init__.py",
    "bili_comments/__main__.py",
    "bili_comments/api.py",
    "bili_comments/cli.py",
    "bili_comments/crawler.py",
    "bili_comments/models.py",
    "bili_comments/storage.py",
    "bili_comments/skills/SKILL.md",
}
WHEEL_METADATA_FILES = {
    "licenses/LICENSE",
    "METADATA",
    "WHEEL",
    "entry_points.txt",
    "top_level.txt",
    "RECORD",
}
SDIST_ROOT_FILES = {
    "LICENSE",
    "MANIFEST.in",
    "PKG-INFO",
    "README.md",
    "README_CN.md",
    "pyproject.toml",
    "setup.cfg",
}
SDIST_EGG_INFO_FILES = {
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "entry_points.txt",
    "top_level.txt",
}
WHEEL_DIST_INFO_RE = re.compile(
    r"^bilibili_crawler_cli-[0-9][A-Za-z0-9_.!+-]*\.dist-info$"
)
SDIST_ROOT_RE = re.compile(
    r"^bilibili_crawler_cli-[0-9][A-Za-z0-9_.!+-]*$"
)

CONTENT_PATTERNS = (
    (
        "POSIX absolute filesystem path",
        re.compile(
            r"(?<![A-Za-z0-9])/"
            r"(?:Users|home|root|private|tmp|var|opt|usr|srv|Volumes|mnt|"
            r"media|data|app|code|repo|repos|workspace|workspaces)"
            r"(?:/[^\s\"'<>`]+)+"
        ),
    ),
    (
        "Windows absolute filesystem path",
        re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/](?:[^\\/\s\"'<>]+[\\/])+"),
    ),
    (
        "file URI",
        re.compile(r"(?i)\bfile:///(?:[^\s\"'<>`]+)"),
    ),
    (
        "private key",
        re.compile(
            r"-----BEGIN (?:RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----"
        ),
    ),
    (
        "GitHub token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    ),
    (
        "PyPI token",
        re.compile(r"\bpypi-[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "AWS access key",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    (
        "HTTP bearer credential",
        re.compile(
            r"(?i)\bAuthorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/=-]{12,}"
        ),
    ),
    (
        "credential embedded in URL",
        re.compile(r"(?i)\bhttps?://[^/\s:@]+:[^/\s@]+@"),
    ),
    (
        "credential assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|"
            r"password|passwd)\s*[:=]\s*['\"]?[A-Za-z0-9._~+/=-]{12,}"
        ),
    ),
    (
        "BILI_COOKIE value",
        re.compile(
            r"\bBILI_COOKIE\s*=\s*['\"]?"
            r"(?!完整|示例|REDACTED|redacted|example|dummy|test|<|\.\.\.)"
            r"[^\s'\"<>]{12,}"
        ),
    ),
    (
        "Bilibili session cookie",
        re.compile(
            r"(?i)\b(?:SESSDATA|bili_jct|DedeUserID|"
            r"DedeUserID__ckMd5|buvid3|buvid4)\s*=\s*"
            r"(?!REDACTED|redacted|example|dummy|test|<|\.\.\.)"
            r"[A-Za-z0-9%._~+/=-]{8,}"
        ),
    ),
)


class AuditError(Exception):
    """Raised when a distribution violates the release boundary."""


def _canonical_member_name(name: str) -> str:
    if "\x00" in name:
        raise AuditError("archive member contains a NUL byte")
    if "\\" in name:
        raise AuditError(f"archive member uses a backslash: {name!r}")
    if name.startswith("/") or name.startswith("//"):
        raise AuditError(f"archive member is absolute: {name!r}")
    if re.match(r"^[A-Za-z]:", name):
        raise AuditError(f"archive member has a drive prefix: {name!r}")

    path_text = name[:-1] if name.endswith("/") else name
    raw_parts = path_text.split("/")
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise AuditError(f"archive member is not normalized: {name!r}")
    path = PurePosixPath(path_text)
    return path.as_posix()


def _audit_member_path(name: str) -> str:
    canonical = _canonical_member_name(name)
    path = PurePosixPath(canonical)
    lowered_parts = [part.casefold() for part in path.parts]
    basename = lowered_parts[-1]

    blocked_directory = next(
        (part for part in lowered_parts if part in FORBIDDEN_DIRECTORIES),
        None,
    )
    if blocked_directory is not None:
        raise AuditError(
            f"{name!r} contains forbidden directory {blocked_directory!r}"
        )
    if basename in FORBIDDEN_DOCUMENTS:
        raise AuditError(f"{name!r} contains an internal document")
    if basename in FORBIDDEN_FILES:
        raise AuditError(f"{name!r} contains a credential or private file")
    if any(basename.endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
        raise AuditError(f"{name!r} has a forbidden generated/private suffix")
    if basename.startswith("test_") and basename.endswith(".py"):
        raise AuditError(f"{name!r} contains a test module")
    return canonical


def _audit_content(member_name: str, data: bytes) -> None:
    if len(data) > MAX_MEMBER_BYTES:
        raise AuditError(
            f"{member_name!r} exceeds the {MAX_MEMBER_BYTES}-byte member limit"
        )
    if b"\x00" in data:
        raise AuditError(f"{member_name!r} contains binary NUL data")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AuditError(f"{member_name!r} is not valid UTF-8 text") from error
    for description, pattern in CONTENT_PATTERNS:
        if pattern.search(text):
            raise AuditError(f"{member_name!r} contains {description}")


def _wheel_members(path: Path) -> Iterator[tuple[str, bytes]]:
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                canonical = _audit_member_path(member.filename)
                if member.is_dir():
                    continue
                if member.flag_bits & 0x1:
                    raise AuditError(f"{member.filename!r} is encrypted")
                mode = member.external_attr >> 16
                if mode and stat.S_ISLNK(mode):
                    raise AuditError(f"{member.filename!r} is a symbolic link")
                if member.file_size > MAX_MEMBER_BYTES:
                    raise AuditError(
                        f"{member.filename!r} exceeds the member size limit"
                    )
                yield canonical, archive.read(member)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise AuditError(f"cannot read wheel {path.name!r}: {error}") from error


def _sdist_members(path: Path) -> Iterator[tuple[str, bytes]]:
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            for member in archive.getmembers():
                canonical = _audit_member_path(member.name)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise AuditError(
                        f"{member.name!r} is not a regular file or directory"
                    )
                if member.size > MAX_MEMBER_BYTES:
                    raise AuditError(
                        f"{member.name!r} exceeds the member size limit"
                    )
                source = archive.extractfile(member)
                if source is None:
                    raise AuditError(f"cannot read {member.name!r}")
                yield canonical, source.read()
    except (OSError, tarfile.TarError) as error:
        raise AuditError(f"cannot read sdist {path.name!r}: {error}") from error


def _report_allowlist_difference(
    archive_name: str,
    actual: set[str],
    expected: set[str],
) -> None:
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    details: list[str] = []
    if unexpected:
        details.append("unexpected: " + ", ".join(unexpected))
    if missing:
        details.append("missing: " + ", ".join(missing))
    if details:
        raise AuditError(
            f"{archive_name!r} does not match the strict path allowlist; "
            + "; ".join(details)
        )


def _audit_wheel_allowlist(path: Path, names: list[str]) -> None:
    dist_info_roots = {
        PurePosixPath(name).parts[0]
        for name in names
        if PurePosixPath(name).parts[0].endswith(".dist-info")
    }
    if len(dist_info_roots) != 1:
        raise AuditError(
            f"{path.name!r} must contain exactly one .dist-info directory"
        )
    dist_info = next(iter(dist_info_roots))
    if WHEEL_DIST_INFO_RE.fullmatch(dist_info) is None:
        raise AuditError(
            f"{path.name!r} has unexpected .dist-info name {dist_info!r}"
        )
    distribution_stem = dist_info.removesuffix(".dist-info")
    if not path.name.startswith(f"{distribution_stem}-"):
        raise AuditError(
            f"{path.name!r} does not match metadata directory {dist_info!r}"
        )

    expected = set(CORE_PACKAGE_FILES)
    expected.update(
        f"{dist_info}/{metadata_file}"
        for metadata_file in WHEEL_METADATA_FILES
    )
    _report_allowlist_difference(path.name, set(names), expected)


def _audit_sdist_allowlist(path: Path, names: list[str]) -> None:
    roots = {PurePosixPath(name).parts[0] for name in names}
    if len(roots) != 1:
        raise AuditError(
            f"{path.name!r} must contain one and only one root directory"
        )
    root = next(iter(roots))
    if SDIST_ROOT_RE.fullmatch(root) is None:
        raise AuditError(f"{path.name!r} has unexpected root {root!r}")
    if path.name != f"{root}.tar.gz":
        raise AuditError(
            f"{path.name!r} does not match source root {root!r}"
        )

    egg_info = f"{root}/bilibili_crawler_cli.egg-info"
    expected = {f"{root}/{name}" for name in SDIST_ROOT_FILES}
    expected.update(f"{root}/{name}" for name in CORE_PACKAGE_FILES)
    expected.update(
        f"{egg_info}/{metadata_file}"
        for metadata_file in SDIST_EGG_INFO_FILES
    )
    _report_allowlist_difference(path.name, set(names), expected)


def audit_archive(path: Path, kind: str) -> tuple[int, int]:
    if path.is_symlink() or not path.is_file():
        raise AuditError(f"distribution is not a regular file: {path.name!r}")
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise AuditError(
            f"{path.name!r} exceeds the {MAX_ARCHIVE_BYTES}-byte archive limit"
        )

    iterator = _wheel_members(path) if kind == "wheel" else _sdist_members(path)
    names: list[str] = []
    seen: set[str] = set()
    total_bytes = 0
    for name, data in iterator:
        folded = name.casefold()
        if folded in seen:
            raise AuditError(f"{path.name!r} contains duplicate member {name!r}")
        seen.add(folded)
        names.append(name)
        total_bytes += len(data)
        if total_bytes > MAX_ARCHIVE_BYTES:
            raise AuditError(
                f"{path.name!r} exceeds the expanded archive size limit"
            )
        _audit_content(name, data)

    if not names:
        raise AuditError(f"{path.name!r} is empty")
    if kind == "wheel":
        _audit_wheel_allowlist(path, names)
    else:
        _audit_sdist_allowlist(path, names)
    return len(names), total_bytes


def _distribution_pair(directory: Path) -> tuple[Path, Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise AuditError(f"distribution directory does not exist: {directory}")

    entries = sorted(directory.iterdir(), key=lambda item: item.name)
    invalid_entries = [
        item.name
        for item in entries
        if item.is_symlink() or not item.is_file()
    ]
    if invalid_entries:
        raise AuditError(
            "distribution directory contains non-regular entries: "
            + ", ".join(invalid_entries)
        )
    files = entries
    wheels = [item for item in files if item.name.endswith(".whl")]
    sdists = [item for item in files if item.name.endswith(".tar.gz")]
    recognized = {*wheels, *sdists}
    unexpected = [item.name for item in files if item not in recognized]
    if unexpected:
        raise AuditError(
            "distribution directory contains unexpected files: "
            + ", ".join(unexpected)
        )
    if len(wheels) != 1 or len(sdists) != 1:
        raise AuditError(
            "distribution directory must contain exactly one wheel and one "
            f"sdist; found {len(wheels)} wheel(s) and {len(sdists)} sdist(s)"
        )
    return wheels[0], sdists[0]


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit one wheel and one sdist without extracting them."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path("dist"),
        help="directory containing exactly one .whl and one .tar.gz",
    )
    options = parser.parse_args(arguments)

    try:
        wheel, sdist = _distribution_pair(options.directory)
        results = (
            ("wheel", wheel, audit_archive(wheel, "wheel")),
            ("sdist", sdist, audit_archive(sdist, "sdist")),
        )
    except (AuditError, OSError) as error:
        print(f"distribution audit failed: {error}", file=sys.stderr)
        return 1

    for kind, path, (members, expanded_bytes) in results:
        print(
            f"{kind}: {path.name}: {members} files, "
            f"{expanded_bytes} expanded bytes"
        )
    print("distribution audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

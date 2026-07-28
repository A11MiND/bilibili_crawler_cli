"""Stable data models exposed by the Bilibili API adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


Cursor: TypeAlias = object


@dataclass(frozen=True, slots=True)
class Author:
    """A Bilibili user as exposed by a public comment response."""

    mid: str
    name: str

    @property
    def user_id(self) -> str:
        """Compatibility alias used by the CSV orchestration layer."""

        return self.mid


@dataclass(frozen=True, slots=True)
class VideoInfo:
    """Identifiers and owner metadata required while crawling one video."""

    aid: int
    bvid: str
    title: str
    owner: Author
    reply_count: int | None = None

    @property
    def canonical_url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}/"

    @property
    def owner_id(self) -> str:
        return self.owner.mid

    @property
    def owner_name(self) -> str:
        return self.owner.name


@dataclass(frozen=True, slots=True)
class Comment:
    """A normalized root comment or child reply.

    ``root`` always identifies the root comment. For a root comment it is
    normalized to the comment's own ``rpid`` even though Bilibili commonly
    returns ``0`` in the raw ``root`` field. ``parent`` is ``None`` only for a
    root comment.
    """

    rpid: str
    root: str
    parent: str | None
    author: Author
    content: str
    ctime: int
    likes: int | None
    location: str | None
    rcount: int
    reply_to_author: Author | None = None

    @property
    def is_root(self) -> bool:
        return self.parent is None

    # These aliases keep the API model pleasant to use from code written
    # against the names in SDD section 4 without duplicating stored fields.
    @property
    def comment_id(self) -> str:
        return self.rpid

    @property
    def root_id(self) -> str:
        return self.root

    @property
    def parent_id(self) -> str | None:
        return self.parent

    @property
    def author_id(self) -> str:
        return self.author.mid

    @property
    def author_name(self) -> str:
        return self.author.name

    @property
    def created_at(self) -> int:
        return self.ctime

    @property
    def like_count(self) -> int | None:
        return self.likes

    @property
    def ip_location(self) -> str | None:
        return self.location

    @property
    def reply_to_author_id(self) -> str | None:
        """Explicit direct-parent author ID when returned by Bilibili."""

        return (
            None
            if self.reply_to_author is None
            else self.reply_to_author.mid
        )

    @property
    def reply_to_author_name(self) -> str | None:
        """Explicit direct-parent author name when returned by Bilibili."""

        return (
            None
            if self.reply_to_author is None
            else self.reply_to_author.name
        )


@dataclass(frozen=True, slots=True)
class CommentPage:
    """One fully decoded API page and its opaque continuation position."""

    items: list[Comment]
    next_cursor: Cursor | None
    has_more: bool

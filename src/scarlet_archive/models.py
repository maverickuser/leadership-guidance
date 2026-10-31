"""Plain data types shared by the stages."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

Status = Literal["discovered", "fetched", "ok", "partial", "failed"]
STATUSES: tuple[str, ...] = ("discovered", "fetched", "ok", "partial", "failed")


# Slugs become file names, so they may only hold these characters (no "/" or "..").
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]*$"
_SLUG_RE = re.compile(SLUG_PATTERN)


def iso_utc(dt: datetime) -> str:
    """UTC timestamp like 2026-10-31T11:03:00Z."""
    return dt.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_iso() -> str:
    return iso_utc(datetime.now(UTC))


def is_valid_slug(slug: object) -> bool:
    return isinstance(slug, str) and _SLUG_RE.match(slug) is not None


@dataclass
class ManifestEntry:
    url: str
    slug: str
    categories: list[str] = field(default_factory=list)
    status: Status = "discovered"
    last_attempt: str | None = None
    error: str | None = None
    content_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: object) -> ManifestEntry:
        """Build an entry from manifest JSON. Raises ValueError if it is malformed."""
        if not isinstance(d, dict):
            raise ValueError("each post must be an object")
        url, slug = d.get("url"), d.get("slug")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError(f"post has no valid url: {url!r}")
        if not is_valid_slug(slug):
            raise ValueError(f"post has an unsafe slug: {slug!r}")
        status = d.get("status", "discovered")
        if status not in STATUSES:
            status = "discovered"
        return cls(
            url=url,
            slug=cast(str, slug),
            categories=list(d.get("categories") or []),
            status=cast(Status, status),
            last_attempt=d.get("last_attempt"),
            error=d.get("error"),
            content_hash=d.get("content_hash"),
        )


@dataclass
class Manifest:
    entries: dict[str, ManifestEntry] = field(default_factory=dict)  # slug -> entry, sitemap order

    def ordered(self) -> list[ManifestEntry]:
        return list(self.entries.values())

    def __len__(self) -> int:
        return len(self.entries)


@dataclass
class Section:
    heading: str | None
    level: int
    text: str


@dataclass
class Extracted:
    title: str
    subtitle: str | None
    author: str
    published_date: str
    summary: str
    sections: list[Section]
    text: str
    links: list[dict[str, str]]
    word_count: int
    paywalled: bool = False  # the paywall box was inside the article body


# Called once per post per stage: (slug, stage, status, error or detail)
PostCallback = Callable[[str, str, str, "str | None"], None]


def no_callback(slug: str, stage: str, status: str, error: str | None = None) -> None:
    return None

"""Plain data types shared by the stages."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable, Literal, Optional

Status = Literal["discovered", "fetched", "ok", "partial", "failed"]
STATUSES: tuple[str, ...] = ("discovered", "fetched", "ok", "partial", "failed")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ManifestEntry:
    url: str
    slug: str
    categories: list[str] = field(default_factory=list)
    status: Status = "discovered"
    last_attempt: str | None = None
    error: str | None = None
    content_hash: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ManifestEntry":
        status = d.get("status", "discovered")
        if status not in STATUSES:
            status = "discovered"
        return cls(
            url=d["url"],
            slug=d["slug"],
            categories=list(d.get("categories") or []),
            status=status,
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
    links: list[dict]
    word_count: int
    paywalled: bool = False  # the paywall box was inside the article body


# Called once per post per stage: (slug, stage, status, error or detail)
PostCallback = Callable[[str, str, str, Optional[str]], None]


def no_callback(slug: str, stage: str, status: str, error: Optional[str] = None) -> None:
    return None

"""Plain data types for indexing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Section:
    heading: str | None
    text: str  # paragraphs separated by blank lines


@dataclass(frozen=True)
class Post:
    """The fields of a post record (``posts/{id}.json``) that indexing uses."""

    id: str
    title: str
    url: str
    status: str
    sections: tuple[Section, ...]
    subtitle: str | None = None
    author: str | None = None
    published_date: str | None = None
    categories: tuple[str, ...] = field(default_factory=tuple)
    word_count: int = 0
    fetched_at: str | None = None

    @classmethod
    def from_record(cls, d: dict[str, Any]) -> Post:
        """Build from a schema-valid post record."""
        return cls(
            id=d["id"],
            title=d["title"],
            url=d["url"],
            status=d["status"],
            sections=tuple(Section(s.get("heading"), s["text"]) for s in d.get("sections", [])),
            subtitle=d.get("subtitle"),
            author=d.get("author"),
            published_date=d.get("published_date"),
            categories=tuple(d.get("categories") or ()),
            word_count=int(d.get("word_count") or 0),
            fetched_at=d.get("fetched_at"),
        )

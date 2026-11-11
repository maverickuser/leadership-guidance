"""Search filters shared by keyword and vector search: categories, date range, kind and newsletter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from psycopg import sql

KIND_VALUES = {"all": None, "passages": "passage", "summaries": "summary"}


@dataclass(frozen=True)
class Filters:
    categories: tuple[str, ...] = ()  # exact names (already resolved against the store)
    since: date | None = None
    until: date | None = None
    kind: str = "all"  # all | passages | summaries
    sources: tuple[str, ...] = ()  # newsletter ids (already checked against the store)

    def __post_init__(self) -> None:
        if self.kind not in KIND_VALUES:
            raise ValueError(f"kind must be one of {', '.join(KIND_VALUES)}, got {self.kind!r}")


def filter_sql(filters: Filters) -> tuple[sql.Composable, list[object]]:
    """SQL conditions (each starting with AND) and their parameters."""
    parts: list[sql.Composable] = []
    params: list[object] = []
    if filters.categories:
        parts.append(sql.SQL("categories && %s"))
        params.append(list(filters.categories))
    if filters.since:
        parts.append(sql.SQL("published_date >= %s"))
        params.append(filters.since)
    if filters.until:
        parts.append(sql.SQL("published_date <= %s"))
        params.append(filters.until)
    if filters.sources:
        parts.append(sql.SQL("source = ANY(%s)"))
        params.append(list(filters.sources))
    kind = KIND_VALUES[filters.kind]
    if kind:
        parts.append(sql.SQL("kind = %s"))
        params.append(kind)
    return sql.SQL("").join(sql.SQL(" AND ") + p for p in parts), params

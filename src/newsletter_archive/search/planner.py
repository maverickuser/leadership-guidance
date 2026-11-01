"""Decide what an index run does (pure; no I/O): which posts are new, changed, unchanged,
removed or skipped, and what it will cost.

Rules (spec search-indexing):
- a post is re-indexed exactly when its fingerprint changed, or it is new;
- posts no longer archived are removed, unless that would remove more than
  ``max(mass_delete_min, mass_delete_fraction × indexed)`` posts (the deletion guard), and
  never posts whose JSON merely failed to load this run;
- a post that failed ``MAX_ATTEMPTS`` times with the same fingerprint is skipped until its
  fingerprint changes or ``retry_failed`` is set; summaries follow the same rule separately;
- an unchanged post missing its summary only needs the summary.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from .models import Post, Section

MAX_ATTEMPTS = 3
SUMMARY_STAGE = "summarize"
SUMMARY_EMBED_TOKENS = 300  # estimate for embedding a summary not written yet


@dataclass(frozen=True)
class Prepared:
    """A post after cleaning, with its fingerprint."""

    post: Post
    sections: list[Section]
    fingerprint: str
    loss_fraction: float = 0.0


@dataclass(frozen=True)
class Stored:
    """What the target generation holds for a post."""

    fingerprint: str
    has_summary: bool


@dataclass(frozen=True)
class Failure:
    stage: str
    attempts: int
    fingerprint: str


@dataclass
class Plan:
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    guarded: list[str] = field(default_factory=list)  # removals the deletion guard stopped
    skipped: list[str] = field(default_factory=list)  # gave up after MAX_ATTEMPTS
    summary_only: list[str] = field(default_factory=list)

    @property
    def to_index(self) -> list[str]:
        return self.new + self.changed


def _gave_up(failures: list[Failure], fingerprint: str, summary: bool) -> bool:
    return any(
        f.attempts >= MAX_ATTEMPTS and f.fingerprint == fingerprint and (f.stage == SUMMARY_STAGE) == summary
        for f in failures
    )


def mass_delete_limit(indexed: int, minimum: int, fraction: float) -> int:
    return max(minimum, math.floor(fraction * indexed))


def make_plan(
    prepared: Mapping[str, Prepared],
    stored: Mapping[str, Stored],
    failures: Mapping[str, list[Failure]],
    *,
    keep: set[str] = frozenset(),  # type: ignore[assignment]
    summaries_enabled: bool = True,
    retry_failed: bool = False,
    allow_mass_delete: bool = False,
    mass_delete_min: int = 5,
    mass_delete_fraction: float = 0.10,
) -> Plan:
    plan = Plan()
    for post_id in sorted(prepared):
        p = prepared[post_id]
        own = failures.get(post_id, [])
        current = stored.get(post_id)
        if current is None or current.fingerprint != p.fingerprint:
            if not retry_failed and _gave_up(own, p.fingerprint, summary=False):
                plan.skipped.append(post_id)
            else:
                (plan.new if current is None else plan.changed).append(post_id)
            continue
        plan.unchanged.append(post_id)
        wants_summary = summaries_enabled and not current.has_summary
        if wants_summary and (retry_failed or not _gave_up(own, p.fingerprint, summary=True)):
            plan.summary_only.append(post_id)
    removed = sorted(set(stored) - set(prepared) - set(keep))
    if (
        len(removed) > mass_delete_limit(len(stored), mass_delete_min, mass_delete_fraction)
        and not allow_mass_delete
    ):
        plan.guarded = removed
    else:
        plan.removed = removed
    return plan


@dataclass(frozen=True)
class Estimate:
    passages: int
    passages_cached: int
    embed_tokens: int
    summaries: int
    summary_tokens: int
    embed_usd_per_million: float
    summary_usd_per_million: float

    @property
    def cost_usd(self) -> float:
        return (
            self.embed_tokens * self.embed_usd_per_million
            + self.summary_tokens * self.summary_usd_per_million
        ) / 1_000_000

    def line(self) -> str:
        return (
            f"to embed: {self.passages - self.passages_cached} of {self.passages} passages "
            f"({self.passages_cached} cached), ~{self.embed_tokens:,} tokens; "
            f"summaries to write: {self.summaries}, ~{self.summary_tokens:,} tokens; "
            f"estimated cost ~${self.cost_usd:.2f}"
        )

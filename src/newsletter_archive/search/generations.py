"""Index versions ("generations"): build a new one beside the live one, catch it up, check it,
swap it in, and roll back (spec search-indexing / Safe index updates).

A new generation is needed only for a new embedding model or dimension (``--refresh``). While it
builds, the active one keeps serving searches and taking weekly updates. Before any swap the
candidate is caught up with posts that changed since it was built, and verified; if an
evaluation is available, a candidate more than ``quality_gate_max_recall_drop`` points worse
than the live one isn't swapped in unless forced. Only the newest retired generation is kept,
for ``--rollback``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import psycopg
from psycopg import sql

from ..config import Config
from ..errors import ArchiveError
from . import db
from .indexer import Indexer, IndexOptions, IndexReport

log = logging.getLogger("newsletter_archive.search.generations")

# (candidate generation, active generation) -> (candidate recall@10, active recall@10), or None
# when no evaluation file exists. Wired to scripts/eval_search.py in PR 10.
QualityGate = Callable[[db.Generation, db.Generation], tuple[float, float] | None]


class GenerationError(ArchiveError):
    """A build, swap or rollback can't go ahead (exit 1)."""


@dataclass
class GenerationResult:
    action: str  # built | swapped | rolled-back
    generation: int
    previous: int | None
    report: IndexReport
    gate: tuple[float, float] | None = None

    def summary(self) -> str:
        verb = {"built": "built (not live yet)", "swapped": "is now live", "rolled-back": "is live again"}
        previous = f" (previous: {self.previous})" if self.previous else ""
        return f"Generation {self.generation} {verb[self.action]}{previous}"


def no_gate(candidate: db.Generation, active: db.Generation) -> tuple[float, float] | None:
    return None


class Generations:
    def __init__(
        self,
        config: Config,
        conn: psycopg.Connection,
        indexer_factory: Callable[[], Indexer],
        gate: QualityGate = no_gate,
    ) -> None:
        self.config = config
        self.conn = conn
        self.indexer_factory = indexer_factory
        self.gate = gate

    # -- commands -------------------------------------------------------------------------------

    def refresh(self, options: IndexOptions, swap: bool = True, force: bool = False) -> GenerationResult:
        """``--refresh``: build a new generation with the configured model beside the live one;
        swap it in unless ``swap`` is False (``--no-swap`` leaves it ``ready`` for ``--swap``)."""
        with db.index_lock(self.conn):  # held for the whole command (re-entrant per session)
            db.apply_migrations(self.conn)
            if not options.dry_run:
                for g in db.list_generations(self.conn):
                    if g.status == "building" and db.active_generation(self.conn) is not None:
                        self._drop(g)  # an earlier --refresh that never finished
            report = self.indexer_factory().run(options, target="new", activate_first_build=False)
            if options.dry_run:
                return GenerationResult("built", report.generation, None, report)
            candidate = self._get(report.generation)
            if report.build_incomplete or report.failed or report.not_attempted:
                raise GenerationError(
                    f"generation {candidate.generation} was built but is incomplete; fix the failures "
                    "and run "
                    "`archive index --refresh` again (search keeps using the current generation)"
                )
            db.set_generation_status(self.conn, candidate.generation, "ready")
            if not swap:
                return GenerationResult("built", candidate.generation, self._active_number(), report)
            return self._swap(self._get(candidate.generation), options, force, report)

    def swap(self, options: IndexOptions, force: bool = False) -> GenerationResult:
        """``--swap``: catch up the newest ``ready`` generation, check it and make it live."""
        with db.index_lock(self.conn):
            ready = [g for g in db.list_generations(self.conn) if g.status == "ready"]
            if not ready:
                raise GenerationError(
                    "no generation is ready to swap in; build one with `archive index --refresh --no-swap`"
                )
            return self._swap(ready[-1], options, force, None)

    def rollback(self, options: IndexOptions) -> GenerationResult:
        """``--rollback``: catch up the newest retired generation and make it live again."""
        with db.index_lock(self.conn):
            retired = [g for g in db.list_generations(self.conn) if g.status == "retired"]
            if not retired:
                raise GenerationError("there is no previous generation to roll back to")
            target = retired[-1]
            self._check_model(target, "--rollback")
            previous = self._active_number()
            report = self._catch_up(target, options)
            db.activate_generation(self.conn, target.generation)
            log.info("rolled back to generation %s", target.generation)
            return GenerationResult("rolled-back", target.generation, previous, report)

    # -- steps ----------------------------------------------------------------------------------

    def _swap(
        self, candidate: db.Generation, options: IndexOptions, force: bool, report: IndexReport | None
    ) -> GenerationResult:
        s = self.config.search
        self._check_model(candidate, "--swap")
        caught_up = self._catch_up(candidate, options)
        report = report or caught_up
        active = db.active_generation(self.conn)
        gate = None
        if active is not None and active.generation != candidate.generation:
            gate = self.gate(candidate, active)
            if gate is None:
                log.warning("no evaluation file: swapping without a quality check")
            elif gate[1] - gate[0] > s.quality_gate_max_recall_drop and not force:
                raise GenerationError(
                    f"generation {candidate.generation} scores recall@10 {gate[0]:.1f} vs {gate[1]:.1f} "
                    "for the live "
                    f"one (more than {s.quality_gate_max_recall_drop:g} points worse); not swapped. "
                    "Use --force to swap anyway."
                )
        db.activate_generation(self.conn, candidate.generation)
        self._drop_old_retired()
        log.info("generation %s is now live", candidate.generation)
        return GenerationResult(
            "swapped", candidate.generation, active.generation if active else None, report, gate
        )

    def _check_model(self, gen: db.Generation, command: str) -> None:
        s = self.config.search
        if (gen.embedding_model, gen.embedding_dim) != (s.embedding_model, s.embedding_dimension):
            raise GenerationError(
                f"generation {gen.generation} uses {gen.embedding_model} ({gen.embedding_dim} dimensions); "
                "set "
                f"search.embedding_model and search.embedding_dimension to match it, then run {command} again"
            )

    def _catch_up(self, target: db.Generation, options: IndexOptions) -> IndexReport:
        """Index whatever changed since the target was built, then verify it (Indexer.run)."""
        report = self.indexer_factory().run(
            IndexOptions(retry_failed=True, allow_mass_delete=options.allow_mass_delete, yes=options.yes),
            target=target,
            activate_first_build=False,
        )
        if report.failed or report.not_attempted:
            raise GenerationError(
                f"generation {target.generation} couldn't be fully caught up "
                f"({len(report.failed) + len(report.not_attempted)} posts); it was not made live"
            )
        return report

    def _drop_old_retired(self) -> None:
        retired = [g for g in db.list_generations(self.conn) if g.status == "retired"]
        for g in retired[:-1]:  # keep only the newest, for rollback
            self._drop(g)

    def _drop(self, gen: db.Generation) -> None:
        with self.conn.transaction():
            self.conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(gen.schema)))
            self.conn.execute("DELETE FROM public.generations WHERE generation = %s", (gen.generation,))
        log.info("dropped generation %s", gen.generation)

    def _get(self, number: int) -> db.Generation:
        for g in db.list_generations(self.conn):
            if g.generation == number:
                return g
        raise GenerationError(f"no generation {number}")

    def _active_number(self) -> int | None:
        active = db.active_generation(self.conn)
        return active.generation if active else None

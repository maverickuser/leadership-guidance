"""Index versions ("generations"): build a new one beside the live one, catch it up, check it,
swap it in, and roll back (spec search-indexing / Safe index updates).

A new generation is needed only for a new embedding model or dimension (``--refresh``). While it
builds, the active one keeps serving searches and taking weekly updates (with its own model).
Before a ``--swap`` or ``--rollback`` the target is caught up with posts that changed since it
was last updated, and verified; if an evaluation is available, a candidate more than
``quality_gate_max_recall_drop`` points worse than the live one isn't swapped in unless forced.

Housekeeping: at most one version is being built or waiting (``ready``) at a time: a new
``--refresh`` resumes an unfinished build with the same model and drops anything else left
over. Only the most recently retired version is kept, for ``--rollback``. Every command
honours ``--dry-run``: it shows what would happen and changes nothing.
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
from .indexer import Indexer, IndexOptions, IndexReport, blocking_failures

log = logging.getLogger("newsletter_archive.search.generations")

# (candidate generation, active generation) -> (candidate recall@10, active recall@10), or None
# when no evaluation file exists. Wired to scripts/eval_search.py in PR 10.
QualityGate = Callable[[db.Generation, db.Generation], tuple[float, float] | None]


class GenerationError(ArchiveError):
    """A build, swap or rollback can't go ahead (exit 1)."""


@dataclass
class GenerationResult:
    action: str  # built | partial | swapped | rolled-back | dry-run
    generation: int | None
    previous: int | None
    report: IndexReport
    gate: tuple[float, float] | None = None
    plan: str = ""  # dry-run: what the command would do

    def summary(self) -> str:
        if self.action == "dry-run":
            return f"Dry run: {self.plan}; nothing was changed"
        if self.action == "partial":
            return (
                f"Generation {self.generation} partly built (--limit); run `archive index --refresh` "
                "again to finish it"
            )
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
        """``--refresh``: build a new generation with the configured model beside the live one
        (or finish an unfinished one); swap it in unless ``swap`` is False (``--no-swap`` leaves
        it ``ready`` for ``--swap``). With ``--limit`` it builds part of it and stops."""
        with db.index_lock(self.conn):  # held for the whole command (re-entrant per session)
            db.apply_migrations(self.conn)
            active = self._active_number()
            resume, stale = self._leftovers()
            target: db.Generation | str = resume or "new"
            if options.dry_run:
                report = self.indexer_factory().run(options, target=target, activate_first_build=False)
                s = self.config.search
                what = (
                    f"would finish building generation {resume.generation}"
                    if resume
                    else f"would build a new index version with {s.embedding_model} ({s.embedding_dimension})"
                )
                if stale:
                    what += f" and drop leftover generation(s) {', '.join(str(g.generation) for g in stale)}"
                return GenerationResult(
                    "dry-run", resume.generation if resume else None, active, report, plan=what
                )
            for g in stale:
                self._drop(g)
            report = self.indexer_factory().run(options, target=target, activate_first_build=False)
            candidate = self._get(report.generation)
            if report.build_incomplete:
                if report.limited and not blocking_failures(report):
                    return GenerationResult("partial", candidate.generation, active, report)
                raise GenerationError(
                    f"generation {candidate.generation} was built but is incomplete; fix the failures "
                    "and run `archive index --refresh` again to finish it (search keeps using the "
                    "current generation)"
                )
            db.set_generation_status(self.conn, candidate.generation, "ready")
            if not swap:
                return GenerationResult("built", candidate.generation, active, report)
            # just built under the same lock: nothing to catch up
            return self._activate(self._get(candidate.generation), force, report)

    def swap(self, options: IndexOptions, force: bool = False) -> GenerationResult:
        """``--swap``: catch up the waiting (``ready``) generation, check it and make it live."""
        with db.index_lock(self.conn):
            active = db.active_generation(self.conn)
            ready = [
                g
                for g in db.list_generations(self.conn)
                if g.status == "ready" and (active is None or g.generation > active.generation)
            ]
            if not ready:
                raise GenerationError(
                    "no generation is ready to swap in; build one with `archive index --refresh --no-swap`"
                )
            candidate = ready[-1]
            self._check_model(candidate, "--swap")
            report = self._catch_up(candidate, options)
            if options.dry_run:
                plan = f"would catch up generation {candidate.generation} and make it live"
                return GenerationResult(
                    "dry-run", candidate.generation, self._active_number(), report, plan=plan
                )
            return self._activate(candidate, force, report)

    def rollback(self, options: IndexOptions) -> GenerationResult:
        """``--rollback``: catch up the generation that was live before this one and restore it."""
        with db.index_lock(self.conn):
            target = db.latest_retired(self.conn)
            if target is None:
                raise GenerationError("there is no previous generation to roll back to")
            self._check_model(target, "--rollback")
            previous = self._active_number()
            report = self._catch_up(target, options)
            if options.dry_run:
                plan = f"would catch up generation {target.generation} and make it live again"
                return GenerationResult("dry-run", target.generation, previous, report, plan=plan)
            db.activate_generation(self.conn, target.generation)
            log.info("rolled back to generation %s", target.generation)
            return GenerationResult("rolled-back", target.generation, previous, report)

    # -- steps ----------------------------------------------------------------------------------

    def _leftovers(self) -> tuple[db.Generation | None, list[db.Generation]]:
        """(an unfinished build with the configured model to resume, or None; other building or
        ready generations that a new --refresh supersedes)."""
        s = self.config.search
        resume = None
        for g in reversed(db.list_generations(self.conn)):
            if g.status == "building" and (g.embedding_model, g.embedding_dim) == (
                s.embedding_model,
                s.embedding_dimension,
            ):
                resume = g
                break
        stale = [
            g for g in db.list_generations(self.conn) if g.status in ("building", "ready") and g != resume
        ]
        return resume, stale

    def _activate(self, candidate: db.Generation, force: bool, report: IndexReport) -> GenerationResult:
        s = self.config.search
        active = db.active_generation(self.conn)
        gate = None
        if active is not None and active.generation != candidate.generation:
            try:
                gate = self.gate(candidate, active)
            except (ArchiveError, psycopg.Error) as e:  # the check itself couldn't run
                if not force:
                    raise GenerationError(f"{e}; not swapped (use --force to swap anyway)") from None
                log.warning("quality check failed (%s); swapping anyway (--force)", e)
            else:
                if gate is None:
                    log.warning("no quality comparison available: swapping without a quality check")
            if gate is not None and gate[1] - gate[0] > s.quality_gate_max_recall_drop and not force:
                raise GenerationError(
                    f"generation {candidate.generation} scores recall@10 {gate[0]:.1f} vs {gate[1]:.1f} "
                    f"for the live one (more than {s.quality_gate_max_recall_drop:g} points worse); "
                    "not swapped. Use --force to swap anyway."
                )
        db.activate_generation(self.conn, candidate.generation)
        self._tidy_up()
        log.info("generation %s is now live", candidate.generation)
        return GenerationResult(
            "swapped", candidate.generation, active.generation if active else None, report, gate
        )

    def _check_model(self, gen: db.Generation, command: str) -> None:
        s = self.config.search
        if (gen.embedding_model, gen.embedding_dim) != (s.embedding_model, s.embedding_dimension):
            raise GenerationError(
                f"generation {gen.generation} uses {gen.embedding_model} ({gen.embedding_dim} dimensions); "
                "set search.embedding_model and search.embedding_dimension to match it, then run "
                f"{command} again"
            )

    def _catch_up(self, target: db.Generation, options: IndexOptions) -> IndexReport:
        """Index whatever changed since the target was last updated, then verify it
        (Indexer.run). Every post must index: a version missing posts never goes live."""
        report = self.indexer_factory().run(
            IndexOptions(
                dry_run=options.dry_run, allow_mass_delete=options.allow_mass_delete, yes=options.yes
            ),
            target=target,
            activate_first_build=False,
        )
        blocking = blocking_failures(report)
        if blocking and not options.dry_run:
            raise GenerationError(
                f"generation {target.generation} couldn't be fully caught up "
                f"({len(blocking)} posts); it was not made live"
            )
        return report

    def _tidy_up(self) -> None:
        """After a swap: keep only the most recently retired generation (for rollback) and drop
        any other version still waiting."""
        keep = db.latest_retired(self.conn)
        for g in db.list_generations(self.conn):
            if g.status in ("retired", "ready") and g != keep:
                self._drop(g)

    def _drop(self, gen: db.Generation) -> None:
        with self.conn.transaction():
            self.conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(gen.schema)))
            self.conn.execute("DELETE FROM public.generations WHERE generation = %s", (gen.generation,))
        log.info("dropped generation %s (%s)", gen.generation, gen.status)

    def _get(self, number: int) -> db.Generation:
        for g in db.list_generations(self.conn):
            if g.generation == number:
                return g
        raise GenerationError(f"no generation {number}")

    def _active_number(self) -> int | None:
        active = db.active_generation(self.conn)
        return active.generation if active else None

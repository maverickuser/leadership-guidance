"""``archive index``: build and update the search store from the archived post JSON.

start → load → clean → fingerprint → plan (guards, spending ceiling, dry run) → for each post:
chunk + summarise + embed + write (one transaction) → remove → verify → report.

The first run builds generation 1 (status ``building``) and activates it once verified; later
runs update the active generation in place. A different embedding model or dimension needs a
new generation (``--refresh``, see ``generations.py``). Failures are per post: they are
recorded and retried next run, and never stop the other posts.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
from pgvector import HalfVector
from psycopg import sql

from ..config import Config
from ..errors import ArchiveError
from ..schema import validate_post
from ..store import load_manifest
from . import db
from .boilerplate import Boilerplate, find_boilerplate
from .chunking import Chunk, chunk_sections
from .embeddings import (
    CachedEmbedder,
    EmbeddingCache,
    EmbeddingError,
    check_input,
    passage_input,
    summary_input,
    text_key,
)
from .fingerprint import post_fingerprint
from .gemini import CircuitOpenError, GeminiAuthError, GeminiError
from .models import Post
from .planner import (
    MAX_ATTEMPTS,
    SUMMARY_EMBED_TOKENS,
    SUMMARY_STAGE,
    Estimate,
    Failure,
    Plan,
    Prepared,
    Stored,
    make_plan,
)
from .summaries import (
    MAX_OUTPUT_TOKENS,
    PROMPT,
    CachedSummarizer,
    SummaryCache,
    SummaryError,
    post_text,
    summary_key,
)
from .text import estimate_tokens, fold

log = logging.getLogger("newsletter_archive.search.indexer")

ELIGIBLE = ("ok", "partial")
STUCK_AFTER_RUNS = 2
PostCallback = Callable[[str, str, str, str | None], None]


def _no_callback(slug: str, stage: str, status: str, detail: str | None = None) -> None:
    return None


class IndexError_(ArchiveError):
    """An index run can't go ahead (exit 1)."""


class ModelChangedError(IndexError_):
    def __init__(self, active: db.Generation, model: str, dim: int) -> None:
        super().__init__(
            f"The search store was built with {active.embedding_model} ({active.embedding_dim} dimensions) "
            f"but the config asks for {model} ({dim}); a new index version is needed: "
            "run `archive index --refresh`"
        )


class SpendingCeilingError(IndexError_):
    pass


class VerifyError(IndexError_):
    pass


@dataclass
class IndexOptions:
    limit: int | None = None
    dry_run: bool = False
    retry_failed: bool = False
    allow_mass_delete: bool = False
    yes: bool = False


@dataclass
class IndexReport:
    generation: int = 0
    mode: str = "incremental"
    indexed: list[str] = field(default_factory=list)
    unchanged: int = 0
    removed: list[str] = field(default_factory=list)
    guarded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)  # post id -> "stage: error"
    summaries_failed: dict[str, str] = field(default_factory=dict)
    boilerplate_paragraphs: int = 0
    heavy_loss: list[str] = field(default_factory=list)
    passages: int = 0
    summaries: int = 0
    embedded: int = 0
    cache_hits: int = 0
    tokens_sent: int = 0
    estimate: Estimate | None = None
    unindexed: list[tuple[str, str | None]] = field(default_factory=list)  # (post id, fetched_at)
    stuck: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    status: str = "ok"

    def counts(self) -> dict[str, Any]:
        return {
            "indexed": len(self.indexed),
            "unchanged": self.unchanged,
            "removed": len(self.removed),
            "guarded": len(self.guarded),
            "skipped": len(self.skipped),
            "failed": len(self.failed),
            "summaries_failed": len(self.summaries_failed),
            "passages": self.passages,
            "summaries": self.summaries,
            "embedded": self.embedded,
            "cache_hits": self.cache_hits,
            "tokens_sent": self.tokens_sent,
        }

    def lines(self) -> list[str]:
        out = [
            f"Index summary (generation {self.generation}, {self.mode}): indexed={len(self.indexed)} "
            f"unchanged={self.unchanged} removed={len(self.removed)} failed={len(self.failed)} "
            f"skipped={len(self.skipped)} passages={self.passages} summaries={self.summaries}",
            f"Gemini: inputs embedded={self.embedded} cache hits={self.cache_hits} "
            f"tokens sent={self.tokens_sent:,}; boilerplate paragraphs removed={self.boilerplate_paragraphs}",
        ]
        if self.estimate:
            out.append(f"Estimate: {self.estimate.line()}")
        if self.guarded:
            out.append(
                f"Deletion guard stopped {len(self.guarded)} removals "
                "(pass --allow-mass-delete if intended): "
                + ", ".join(self.guarded[:10])
                + (" …" if len(self.guarded) > 10 else "")
            )
        for post_id, problem in sorted(self.failed.items()):
            out.append(f"Failed: {post_id}: {problem}")
        for post_id, problem in sorted(self.summaries_failed.items()):
            out.append(f"Summary missing: {post_id}: {problem}")
        for post_id in self.heavy_loss:
            out.append(f"Warning: boilerplate filter removed over 20% of {post_id}")
        for post_id, fetched in self.unindexed:
            out.append(f"Not indexed yet: {post_id} (fetched {utc_age(fetched)})")
        for post_id in self.stuck:
            out.append(f"Stuck: {post_id} has not indexed in {STUCK_AFTER_RUNS}+ runs")
        out.append(f"Index status: {self.status} in {self.elapsed_seconds:.1f}s")
        return out


@dataclass
class _Loaded:
    prepared: dict[str, Prepared]
    load_failed: dict[str, str]
    left_out: set[str]
    fetched_at: dict[str, str | None]
    boilerplate: Boilerplate


def load_posts(config: Config) -> tuple[dict[str, Post], dict[str, str]]:
    """Every eligible post from the manifest and posts/*.json, in manifest order:
    (posts, load failures)."""
    manifest = load_manifest(config.manifest_path)
    posts: dict[str, Post] = {}
    failed: dict[str, str] = {}
    for entry in [e for e in manifest.ordered() if e.status in ELIGIBLE]:
        path = config.post_path(entry.slug)
        try:
            record = json.loads(Path(path).read_text(encoding="utf-8"))
            validate_post(record)
            posts[record["id"]] = Post.from_record(record)
        except (OSError, ValueError, ArchiveError) as e:
            failed[entry.slug] = f"{type(e).__name__}: {e}"[:300]
    return posts, failed


class Indexer:
    """One index run. ``embedder`` and ``summarizer`` are created only if the run needs them,
    so a dry run never needs the API key."""

    def __init__(
        self,
        config: Config,
        conn: psycopg.Connection,
        embedder_factory: Callable[[], Any],
        summarizer_factory: Callable[[], Any] | None,
        on_post: PostCallback = _no_callback,
    ) -> None:
        self.config = config
        self.s = config.search
        self.conn = conn
        self.embedder_factory = embedder_factory
        self.summarizer_factory = summarizer_factory if self.s.summaries.enabled else None
        self.on_post = on_post
        self._embedder: CachedEmbedder | None = None
        self._summarizer: CachedSummarizer | None = None

    # -- run ------------------------------------------------------------------------------------

    def run(self, options: IndexOptions) -> IndexReport:
        started = time.monotonic()
        report = IndexReport()
        with db.index_lock(self.conn):
            db.apply_migrations(self.conn)  # schema setup only; a dry run writes no data
            if not options.dry_run:
                self._abort_stale_runs()
            loaded = self._load(options.limit, report, record=not options.dry_run)
            gen, report.mode = self._target()  # nothing is created until the checks below pass
            report.generation = gen.generation if gen else 0
            stored = self._stored(gen) if gen else {}
            failures = self._failures()
            plan = make_plan(
                loaded.prepared,
                stored,
                failures,
                keep=set(loaded.load_failed) | loaded.left_out,
                summaries_enabled=self.s.summaries.enabled,
                retry_failed=options.retry_failed,
                allow_mass_delete=options.allow_mass_delete,
                mass_delete_min=self.s.mass_delete_min,
                mass_delete_fraction=self.s.mass_delete_fraction,
            )
            report.unchanged, report.guarded, report.skipped = len(plan.unchanged), plan.guarded, plan.skipped
            chunks = {pid: self._chunks(loaded.prepared[pid]) for pid in plan.to_index}
            report.estimate = self._estimate(loaded, plan, chunks)
            if options.dry_run:
                self._dry_run_lines(plan, report)
                report.status = "dry-run"
                report.elapsed_seconds = time.monotonic() - started
                return report
            self._check_ceiling(report.estimate, options.yes)
            if gen is None:
                gen = db.create_generation(self.conn, self.s.embedding_model, self.s.embedding_dimension)
                report.generation = gen.generation
            run_id = self._start_run(report.mode, gen.generation)
            try:
                self._index(gen, loaded, plan, chunks, report)
                self._remove(gen, plan.removed, report)
                self._save_boilerplate(gen, loaded.boilerplate)
                self._verify(gen, loaded, report)
                if gen.status == "building":
                    db.set_generation_status(self.conn, gen.generation, "ready")
                    db.activate_generation(self.conn, gen.generation)
                    log.info("generation %s is now active", gen.generation)
                self._freshness(gen, loaded, report)
                self._gc()
                report.status = "partial" if report.failed or report.summaries_failed else "ok"
            except BaseException as e:
                report.status = "failed"
                self._finish_run(run_id, report, f"{type(e).__name__}: {e}")
                raise
            report.elapsed_seconds = time.monotonic() - started
            self._finish_run(run_id, report, None)
        return report

    # -- steps ----------------------------------------------------------------------------------

    def _load(self, limit: int | None, report: IndexReport, record: bool = True) -> _Loaded:
        # Boilerplate is always found across every eligible post, so --limit never changes
        # fingerprints; --limit only chooses which posts this run indexes (the rest are kept).
        posts, load_failed = load_posts(self.config)
        for slug, problem in load_failed.items():
            report.failed[slug] = f"load: {problem}"
            if record:
                self._record_failure(slug, "load", problem, "")
        boilerplate = find_boilerplate(
            {pid: list(p.sections) for pid, p in posts.items()},
            min_posts=self.s.boilerplate_min_posts,
            similarity=self.s.boilerplate_similarity,
        )
        chosen = list(posts)[:limit] if limit else list(posts)
        left_out = set(posts) - set(chosen)
        prepared: dict[str, Prepared] = {}
        for pid in chosen:
            post = posts[pid]
            cleaned = boilerplate.clean(post.sections, pid)
            report.boilerplate_paragraphs += cleaned.removed_paragraphs
            if cleaned.loss_fraction > 0.2:
                report.heavy_loss.append(pid)
            fp = post_fingerprint(post, cleaned.sections, self.s)
            prepared[pid] = Prepared(post, cleaned.sections, fp, cleaned.loss_fraction)
        fetched = {pid: p.fetched_at for pid, p in posts.items()}
        return _Loaded(prepared, load_failed, left_out, fetched, boilerplate)

    def _target(self) -> tuple[db.Generation | None, str]:
        """The generation to update; None when the first build still has to create one."""
        active = db.active_generation(self.conn)
        model, dim = self.s.embedding_model, self.s.embedding_dimension
        if active is not None:
            if (active.embedding_model, active.embedding_dim) != (model, dim):
                raise ModelChangedError(active, model, dim)
            return active, "incremental"
        for gen in reversed(db.list_generations(self.conn)):
            if gen.status == "building" and (gen.embedding_model, gen.embedding_dim) == (model, dim):
                log.info("resuming the first build of generation %s", gen.generation)
                return gen, "build"
        return None, "build"

    def _stored(self, gen: db.Generation) -> dict[str, Stored]:
        rows = self.conn.execute(
            sql.SQL("SELECT post_id, fingerprint, summary_sha256 IS NOT NULL FROM {}.posts").format(
                sql.Identifier(gen.schema)
            )
        ).fetchall()
        return {r[0]: Stored(r[1], bool(r[2])) for r in rows}

    def _failures(self) -> dict[str, list[Failure]]:
        out: dict[str, list[Failure]] = {}
        for post_id, stage, attempts, fp in self.conn.execute(
            "SELECT post_id, stage, attempts, fingerprint FROM public.index_failures"
        ).fetchall():
            out.setdefault(post_id, []).append(Failure(stage, attempts, fp))
        return out

    def _chunks(self, p: Prepared) -> list[Chunk]:
        return chunk_sections(p.sections, self.s.chunk_words, self.s.chunk_overlap_words)

    def _inputs(self, p: Prepared, chunks: list[Chunk]) -> list[str]:
        return [passage_input(p.post.title, c.heading, c.text) for c in chunks]

    def _estimate(self, loaded: _Loaded, plan: Plan, chunks: dict[str, list[Chunk]]) -> Estimate:
        """Tokens and cost for what isn't cached yet. Read-only: a dry run changes nothing."""
        texts = [t for pid in plan.to_index for t in self._inputs(loaded.prepared[pid], chunks[pid])]
        keys = {text_key(t): t for t in texts}
        cache = EmbeddingCache(self.conn, self.s.embedding_model, self.s.embedding_dimension)
        cached = cache.has_many(sorted(keys))
        missing = {k: t for k, t in keys.items() if k not in cached}
        summaries_needed = summary_tokens = 0
        if self.s.summaries.enabled:
            sc = self.s.summaries
            wanted: dict[str, str] = {}
            for pid in plan.to_index + plan.summary_only:
                p = loaded.prepared[pid]
                wanted[summary_key(sc.model, sc.thinking_level, p.post.title, p.sections)] = post_text(
                    p.post.title, p.sections
                )
            have = SummaryCache(self.conn).has_many(sorted(wanted))
            todo = [text for key, text in wanted.items() if key not in have]
            summaries_needed = len(todo)
            summary_tokens = sum(
                estimate_tokens(PROMPT) + estimate_tokens(t) + MAX_OUTPUT_TOKENS for t in todo
            )
        return Estimate(
            passages=len(texts),
            passages_cached=sum(1 for t in texts if text_key(t) in cached),
            embed_tokens=sum(estimate_tokens(t) for t in missing.values())
            + summaries_needed * SUMMARY_EMBED_TOKENS,
            summaries=summaries_needed,
            summary_tokens=summary_tokens,
            embed_usd_per_million=self.s.embed_usd_per_million_tokens,
            summary_usd_per_million=self.s.summary_usd_per_million_tokens,
        )

    def _check_ceiling(self, estimate: Estimate, yes: bool) -> None:
        over = []
        if estimate.embed_tokens > self.s.max_embed_tokens_per_run:
            over.append(f"embedding ~{estimate.embed_tokens:,} > {self.s.max_embed_tokens_per_run:,} tokens")
        if estimate.summary_tokens > self.s.summaries.max_tokens_per_run:
            over.append(
                f"summaries ~{estimate.summary_tokens:,} > {self.s.summaries.max_tokens_per_run:,} tokens"
            )
        if over and not yes:
            raise SpendingCeilingError(
                f"This run is over the spending ceiling ({'; '.join(over)}; ~${estimate.cost_usd:.2f}). "
                "Check with --dry-run, then re-run with --yes to go ahead."
            )

    def _dry_run_lines(self, plan: Plan, report: IndexReport) -> None:
        for pid in plan.new:
            self.on_post(pid, "index", "would index", "new")
        for pid in plan.changed:
            self.on_post(pid, "index", "would index", "changed")
        for pid in plan.summary_only:
            self.on_post(pid, "index", "would summarise", None)
        for pid in plan.removed:
            self.on_post(pid, "index", "would remove", None)

    def _index(
        self,
        gen: db.Generation,
        loaded: _Loaded,
        plan: Plan,
        chunks: dict[str, list[Chunk]],
        report: IndexReport,
    ) -> None:
        if not (plan.to_index or plan.summary_only):
            return  # nothing to send: don't even create the API clients
        raw = self.embedder_factory()
        embedder = self._embedder = CachedEmbedder(raw, EmbeddingCache(self.conn, raw.model, raw.dim))
        summarizer: CachedSummarizer | None = None
        if self.summarizer_factory is not None:
            summarizer = self._summarizer = CachedSummarizer(
                self.summarizer_factory(), SummaryCache(self.conn)
            )
        breaker_open = False
        for pid in plan.to_index + plan.summary_only:
            p = loaded.prepared[pid]
            summary_only = pid in plan.summary_only
            if breaker_open:
                self._fail(
                    pid,
                    SUMMARY_STAGE if summary_only else "embed",
                    "Gemini calls stopped for this run",
                    p,
                    report,
                )
                continue
            try:
                post_chunks = [] if summary_only else chunks[pid]
                texts = self._inputs(p, post_chunks)
                for text in texts:
                    check_input(text)
                summary: tuple[str, str] | None = None
                if summarizer is not None:
                    try:
                        summary = summarizer.summarize(p.post.title, p.sections)
                    except CircuitOpenError:
                        raise
                    except (SummaryError, GeminiError) as e:
                        if isinstance(e, GeminiAuthError):
                            raise
                        report.summaries_failed[pid] = str(e)
                        self._record_failure(pid, SUMMARY_STAGE, str(e), p.fingerprint)
                        self.on_post(pid, "summarize", "failed", str(e))
                if summary is not None:
                    texts.append(summary_input(p.post.title, summary[0]))
                if summary_only and summary is None:
                    continue
                vectors = embedder.embed(texts)
                if summary_only:
                    assert summary is not None
                    self._write_summary(gen, p, summary, vectors[-1])
                else:
                    self._write_post(gen, p, post_chunks, vectors, summary)
                    report.indexed.append(pid)
                    report.passages += len(post_chunks)
                report.summaries += 1 if summary is not None else 0
                self._clear_failures(pid, summary_ok=summary is not None or summarizer is None)
                self.on_post(pid, "index", "summarised" if summary_only else "indexed", None)
            except GeminiAuthError:
                raise
            except CircuitOpenError as e:
                breaker_open = True
                self._fail(pid, SUMMARY_STAGE if summary_only else "embed", str(e), p, report)
            except EmbeddingError as e:
                stage = "chunk" if "not sent" in str(e) or "empty" in str(e) else "embed"
                self._fail(pid, SUMMARY_STAGE if summary_only else stage, str(e), p, report)
            except GeminiError as e:
                self._fail(pid, SUMMARY_STAGE if summary_only else "embed", str(e), p, report)
            except psycopg.Error as e:
                self._fail(pid, "write", f"{type(e).__name__}: {e}", p, report)
        if summarizer is not None:
            summarizer.flush()
        st = embedder.stats
        report.cache_hits, report.tokens_sent, report.embedded = st.cache_hits, st.tokens, st.embedded

    def _fail(self, pid: str, stage: str, problem: str, p: Prepared, report: IndexReport) -> None:
        if stage == SUMMARY_STAGE:
            report.summaries_failed[pid] = problem
        else:
            report.failed[pid] = f"{stage}: {problem}"
        self._record_failure(pid, stage, problem, p.fingerprint)
        self.on_post(pid, stage, "failed", problem)

    def _post_row(self, p: Prepared, summary_key: str | None) -> tuple[Any, ...]:
        post = p.post
        return (
            post.id,
            post.title,
            post.subtitle,
            post.url,
            post.author,
            post.published_date,
            list(post.categories),
            post.word_count,
            post.status,
            p.fingerprint,
            summary_key,
            post.fetched_at,
        )

    def _passage_row(
        self,
        p: Prepared,
        passage_id: str,
        kind: str,
        n: int,
        heading: str | None,
        body: str,
        text: str,
        vec: np.ndarray,
    ) -> tuple[Any, ...]:
        return (
            passage_id,
            p.post.id,
            kind,
            n,
            heading,
            body,
            p.post.title,
            fold(p.post.title),
            fold(heading) if heading else None,
            fold(body),
            p.post.published_date,
            list(p.post.categories),
            len(body.split()),
            text_key(text),
            HalfVector(vec),
        )

    def _write_post(
        self,
        gen: db.Generation,
        p: Prepared,
        chunks: Sequence[Chunk],
        vectors: Sequence[np.ndarray],
        summary: tuple[str, str] | None,
    ) -> None:
        schema = sql.Identifier(gen.schema)
        texts = self._inputs(p, list(chunks))
        rows = [
            self._passage_row(p, f"{p.post.id}#{c.n}", "passage", c.n, c.heading, c.text, t, v)
            for c, t, v in zip(chunks, texts, vectors, strict=False)
        ]
        if summary is not None:
            rows.append(
                self._passage_row(
                    p, f"{p.post.id}#summary", "summary", -1, None, summary[0],
                    summary_input(p.post.title, summary[0]), vectors[-1],
                )
            )  # fmt: skip
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(sql.SQL("DELETE FROM {}.passages WHERE post_id = %s").format(schema), (p.post.id,))
            cur.execute(
                _UPSERT_POST.format(schema=schema), self._post_row(p, summary[1] if summary else None)
            )
            cur.executemany(_INSERT_PASSAGE.format(schema=schema), rows)

    def _write_summary(
        self, gen: db.Generation, p: Prepared, summary: tuple[str, str], vec: np.ndarray
    ) -> None:
        schema = sql.Identifier(gen.schema)
        row = self._passage_row(
            p,
            f"{p.post.id}#summary",
            "summary",
            -1,
            None,
            summary[0],
            summary_input(p.post.title, summary[0]),
            vec,
        )
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(
                sql.SQL("DELETE FROM {}.passages WHERE post_id = %s AND kind = 'summary'").format(schema),
                (p.post.id,),
            )
            cur.execute(_INSERT_PASSAGE.format(schema=schema), row)
            cur.execute(
                sql.SQL("UPDATE {}.posts SET summary_sha256 = %s WHERE post_id = %s").format(schema),
                (summary[1], p.post.id),
            )

    def _remove(self, gen: db.Generation, removed: list[str], report: IndexReport) -> None:
        if not removed:
            return
        self.conn.execute(
            sql.SQL("DELETE FROM {}.posts WHERE post_id = ANY(%s)").format(sql.Identifier(gen.schema)),
            (removed,),
        )
        self.conn.execute("DELETE FROM public.index_failures WHERE post_id = ANY(%s)", (removed,))
        report.removed = removed
        for pid in removed:
            self.on_post(pid, "index", "removed", None)

    def _save_boilerplate(self, gen: db.Generation, boilerplate: Boilerplate) -> None:
        schema = sql.Identifier(gen.schema)
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(sql.SQL("DELETE FROM {}.boilerplate").format(schema))
            cur.executemany(
                sql.SQL(
                    "INSERT INTO {}.boilerplate (cluster_id, para_sha256, post_count, sample) "
                    "VALUES (%s, %s, %s, %s)"
                ).format(schema),
                [(c.cluster_id, key, c.post_count, c.sample) for c in boilerplate.clusters for key in c.keys],
            )

    def _verify(self, gen: db.Generation, loaded: _Loaded, report: IndexReport) -> None:
        stored = self._stored(gen)
        skip = set(report.failed) | set(report.skipped)
        missing = Stored("", False)
        wrong = [
            pid
            for pid, p in loaded.prepared.items()
            if pid not in skip and stored.get(pid, missing).fingerprint != p.fingerprint
        ]
        if wrong:
            raise VerifyError(
                f"{len(wrong)} posts don't match the archive after indexing: {', '.join(wrong[:5])}"
            )
        schema = sql.Identifier(gen.schema)
        empty = self.conn.execute(
            sql.SQL(
                "SELECT p.post_id FROM {s}.posts p WHERE NOT EXISTS "
                "(SELECT 1 FROM {s}.passages x WHERE x.post_id = p.post_id AND x.kind = 'passage')"
            ).format(s=schema)
        ).fetchall()
        if empty:
            raise VerifyError(f"{len(empty)} indexed posts have no passages: {empty[0][0]}")
        sample = self.conn.execute(
            sql.SQL("SELECT passage_id, search_body, embedding FROM {}.passages LIMIT 1").format(schema)
        ).fetchone()
        if sample is None:
            return
        word = next((w for w in sample[1].split() if w.isalpha() and len(w) > 3), None)
        if word:
            hit = self.conn.execute(
                sql.SQL("SELECT 1 FROM {}.passages WHERE search_body ||| %s LIMIT 1").format(schema), (word,)
            ).fetchone()
            if hit is None:
                raise VerifyError("the keyword index returned nothing for a word that is in the store")
        near = self.conn.execute(
            sql.SQL("SELECT passage_id FROM {}.passages ORDER BY embedding <=> %s LIMIT 1").format(schema),
            (sample[2],),
        ).fetchone()
        if near is None:
            raise VerifyError("vector search returned nothing")

    def _freshness(self, gen: db.Generation, loaded: _Loaded, report: IndexReport) -> None:
        stored = self._stored(gen)
        failures = self._failures()
        eligible = set(loaded.prepared) | set(loaded.load_failed)
        for pid in sorted(eligible - set(stored)):
            report.unindexed.append((pid, loaded.fetched_at.get(pid)))
            attempts = max((f.attempts for f in failures.get(pid, []) if f.stage != SUMMARY_STAGE), default=0)
            if attempts >= STUCK_AFTER_RUNS:
                report.stuck.append(pid)

    def _gc(self) -> None:
        days = self.s.cache_retention_days
        schemas = [g.schema for g in db.list_generations(self.conn)]  # never empty: we just indexed one
        refs = sql.SQL(" UNION ALL ").join(
            sql.SQL("SELECT embed_sha256 AS k FROM {}.passages").format(sql.Identifier(s)) for s in schemas
        )
        post_refs = sql.SQL(" UNION ALL ").join(
            sql.SQL("SELECT summary_sha256 AS k FROM {}.posts").format(sql.Identifier(s)) for s in schemas
        )
        self.conn.execute(
            sql.SQL(
                "DELETE FROM public.embedding_cache WHERE last_used_at < now() - make_interval(days => %s) "
                "AND embed_sha256 NOT IN (SELECT k FROM ({}) r WHERE k IS NOT NULL)"
            ).format(refs),
            (days,),
        )
        self.conn.execute(
            sql.SQL(
                "DELETE FROM public.summary_cache WHERE last_used_at < now() - make_interval(days => %s) "
                "AND summary_sha256 NOT IN (SELECT k FROM ({}) r WHERE k IS NOT NULL)"
            ).format(post_refs),
            (days,),
        )

    # -- bookkeeping ----------------------------------------------------------------------------

    def _abort_stale_runs(self) -> None:
        n = self.conn.execute(
            "UPDATE public.index_runs SET status = 'aborted', finished_at = now(), "
            "error = coalesce(error, 'run ended without finishing (crash or kill)') WHERE status = 'running'"
        ).rowcount
        if n:
            log.warning("marked %s unfinished index run(s) as aborted", n)

    def _start_run(self, mode: str, generation: int) -> int:
        row = self.conn.execute(
            "INSERT INTO public.index_runs (mode, generation) VALUES (%s, %s) RETURNING run_id",
            (mode, generation),
        ).fetchone()
        assert row is not None
        return int(row[0])

    def _finish_run(self, run_id: int, report: IndexReport, error: str | None) -> None:
        if self.conn.closed:
            return
        try:
            self.conn.execute(
                "UPDATE public.index_runs SET finished_at = now(), status = %s, counts = %s, error = %s "
                "WHERE run_id = %s",
                (report.status, json.dumps(report.counts()), error, run_id),
            )
        except psycopg.Error:
            log.warning("could not record the end of index run %s", run_id)

    def _record_failure(self, pid: str, stage: str, problem: str, fingerprint: str) -> None:
        self.conn.execute(
            "INSERT INTO public.index_failures (post_id, stage, error, fingerprint) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (post_id, stage) DO UPDATE SET error = EXCLUDED.error, last_attempt = now(), "
            "attempts = CASE WHEN index_failures.fingerprint = EXCLUDED.fingerprint "
            "THEN index_failures.attempts + 1 ELSE 1 END, fingerprint = EXCLUDED.fingerprint",
            (pid, stage, problem[:2000], fingerprint),
        )

    def _clear_failures(self, pid: str, summary_ok: bool) -> None:
        if summary_ok:
            self.conn.execute("DELETE FROM public.index_failures WHERE post_id = %s", (pid,))
        else:
            self.conn.execute(
                "DELETE FROM public.index_failures WHERE post_id = %s AND stage <> %s", (pid, SUMMARY_STAGE)
            )


_UPSERT_POST = sql.SQL(
    "INSERT INTO {schema}.posts (post_id, title, subtitle, url, author, published_date, categories, "
    "word_count, status, fingerprint, summary_sha256, fetched_at, indexed_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()) ON CONFLICT (post_id) DO UPDATE SET "
    "title = EXCLUDED.title, subtitle = EXCLUDED.subtitle, url = EXCLUDED.url, author = EXCLUDED.author, "
    "published_date = EXCLUDED.published_date, categories = EXCLUDED.categories, "
    "word_count = EXCLUDED.word_count, status = EXCLUDED.status, fingerprint = EXCLUDED.fingerprint, "
    "summary_sha256 = EXCLUDED.summary_sha256, fetched_at = EXCLUDED.fetched_at, indexed_at = now()"
)
_INSERT_PASSAGE = sql.SQL(
    "INSERT INTO {schema}.passages (passage_id, post_id, kind, n, heading, body, title, search_title, "
    "search_heading, search_body, published_date, categories, word_count, embed_sha256, embedding) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)


def utc_age(iso: str | None) -> str:
    if not iso:
        return "unknown"
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    hours = (datetime.now(UTC) - then).total_seconds() / 3600
    return f"{hours / 24:.0f} days ago" if hours >= 48 else f"{hours:.0f} hours ago"


__all__ = [
    "MAX_ATTEMPTS",
    "IndexError_",
    "IndexOptions",
    "IndexReport",
    "Indexer",
    "ModelChangedError",
    "SpendingCeilingError",
    "VerifyError",
    "load_posts",
]


def index_archive(config: Config, options: IndexOptions, on_post: PostCallback = _no_callback) -> IndexReport:
    """Run ``archive index``: real Gemini clients (created only when needed), writer connection.

    Raises:
        MissingGeminiKeyError: no key and the run isn't a dry run (nothing is changed).
        DatabaseError, IndexBusyError, ModelChangedError, SpendingCeilingError, VerifyError.
    """
    from .embeddings import GeminiEmbedder
    from .gemini import GeminiClient, MissingGeminiKeyError
    from .summaries import GeminiSummarizer

    s = config.search
    if not options.dry_run and not config.gemini_api_key:
        raise MissingGeminiKeyError()

    def embedder() -> GeminiEmbedder:
        client = GeminiClient(
            config.gemini_api_key,
            rpm=s.embed_rpm,
            tpm=s.embed_tpm,
            breaker_failures=s.circuit_breaker_failures,
            timeout_seconds=s.gemini_timeout_seconds,
        )
        return GeminiEmbedder(client, s.embedding_model, s.embedding_dimension, s.embed_batch_size)

    def summarizer() -> GeminiSummarizer:
        client = GeminiClient(
            config.gemini_api_key,
            rpm=s.summary_rpm,
            tpm=s.summary_tpm,
            breaker_failures=s.circuit_breaker_failures,
            timeout_seconds=s.gemini_timeout_seconds,
        )
        return GeminiSummarizer(client, s.summaries.model, s.summaries.thinking_level)

    with db.connect(config, "writer") as conn:
        return Indexer(config, conn, embedder, summarizer, on_post).run(options)

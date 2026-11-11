"""`archive migrate`: move a single-newsletter archive into its newsletter's own folder.

The old layout kept `manifest.json`, `raw/` and `posts/` at the top of output_dir, with post
ids equal to slugs. The move copies them into `{output_dir}/{id}/`, rewrites each post file
to schema version 2 (`source`, `id = {id}/{slug}`), prefixes the post ids in the eval files,
and only then deletes the old files, the manifest last. Stopped at any point, it can simply
be run again. postgres/, models/ and logs/ are never touched.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import Config, SourceConfig
from .errors import ArchiveError
from .schema import SCHEMA_VERSION, validate_post
from .search.evaluate import EVAL_FILE
from .search.judge import JUDGE_FILE, load_judgments, save_judgments
from .store import run_lock, write_json_atomic, write_text_atomic

log = logging.getLogger("newsletter_archive.migrate")

EXAMPLES = 3  # paths shown per kind in a dry run

_INLINE_EXPECTED = re.compile(r"^(?P<head>\s*expected:\s*)\[(?P<items>[^\]]*)\](?P<tail>\s*(#.*)?)$")
_BLOCK_EXPECTED = re.compile(r"^(?P<indent>\s*)expected:\s*(#.*)?$")
_BLOCK_ITEM = re.compile(r"^(?P<head>(?P<indent>\s*)-\s+)(?P<item>[^#\s][^#]*?)(?P<tail>\s*(#.*)?)$")


@dataclass
class MigrateResult:
    """What a move did, or would do in a dry run."""

    nothing_to_move: bool = False
    manifest: bool = False
    raw: int = 0
    posts: int = 0
    eval_ids: int = 0  # post ids prefixed across the eval files
    examples: dict[str, list[str]] = field(default_factory=dict)

    def lines(self, source: SourceConfig, dry_run: bool) -> list[str]:
        if self.nothing_to_move:
            return ["Nothing to move: no single-newsletter archive found (already moved?)."]
        verb = "Would move" if dry_run else "Moved"
        out = [
            f"{verb} into {source.id}/: the manifest, {self.raw} saved pages and {self.posts} posts; "
            f"{self.eval_ids} post ids in the eval files {'would be' if dry_run else 'were'} renamed "
            f"to {source.id}/<slug>."
        ]
        for kind, paths in self.examples.items():
            out += [f"  {kind}: {p}" for p in paths]
        if not dry_run:
            out.append("Next: start the database, then `archive index --dry-run` and `archive index`.")
        return out


def _prefixed(post_id: str, source_id: str) -> str:
    return post_id if "/" in post_id else f"{source_id}/{post_id}"


def _rewrite_item(item: str, source_id: str) -> str:
    """One YAML list item (bare or quoted) with its post id prefixed."""
    raw = item.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return f"{raw[0]}{_prefixed(raw[1:-1], source_id)}{raw[0]}"
    return _prefixed(raw, source_id)


def prefix_eval_queries(text: str, source_id: str) -> tuple[str, int]:
    """eval-queries.yaml with every `expected` post id prefixed, comments and layout kept.

    Returns (new text, ids changed).

    Raises:
        ArchiveError: the rewrite would change anything but those ids (checked by parsing both).
    """
    out: list[str] = []
    changed = 0
    block_indent: int | None = None
    for line in text.splitlines(keepends=True):
        body, nl = (line[:-1], "\n") if line.endswith("\n") else (line, "")
        if block_indent is not None:
            m = _BLOCK_ITEM.match(body)
            if m and len(m["indent"]) >= block_indent and ":" not in m["item"]:
                new_item = _rewrite_item(m["item"], source_id)
                changed += new_item != m["item"].strip()
                out.append(f"{m['head']}{new_item}{m['tail']}{nl}")
                continue
            block_indent = None
        if m := _INLINE_EXPECTED.match(body):
            items = [i for i in m["items"].split(",") if i.strip()]
            new_items = [_rewrite_item(i, source_id) for i in items]
            changed += sum(a != b.strip() for a, b in zip(new_items, items, strict=True))
            out.append(f"{m['head']}[{', '.join(new_items)}]{m['tail']}{nl}")
            continue
        if m := _BLOCK_EXPECTED.match(body):
            block_indent = len(m["indent"])
        out.append(line)
    new_text = "".join(out)
    if _expected_ids(new_text) != [[_prefixed(i, source_id) for i in ids] for ids in _expected_ids(text)]:
        raise ArchiveError(
            f"could not rewrite {EVAL_FILE} safely; prefix its post ids with {source_id}/ by hand"
        )
    return new_text, changed


def _expected_ids(text: str) -> list[list[str]]:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ArchiveError(f"{EVAL_FILE} is not valid YAML: {e}") from None
    questions = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(questions, list):
        return []
    return [[str(e) for e in (q.get("expected") or [])] for q in questions if isinstance(q, dict)]


def _v2_record(old: dict[str, Any], source: SourceConfig, slug: str) -> dict[str, Any]:
    """A version-1 post record as version 2: `source` added, `id` = `{source}/{slug}`."""
    if old.get("schema_version") not in (1, SCHEMA_VERSION):
        raise ArchiveError(f"posts/{slug}.json has an unknown schema_version")
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": source.post_id(slug),
        "source": source.id,
    }
    record.update({k: v for k, v in old.items() if k not in record})
    validate_post(record)
    return record


def _same_bytes(a: Path, b: Path) -> bool:
    return b.exists() and a.read_bytes() == b.read_bytes()


def migrate(config: Config, source_id: str, dry_run: bool = False) -> MigrateResult:
    """Move the single-newsletter archive in config.output_dir into source `source_id`'s folder.

    Raises:
        ConfigError: no such source.
        ArchiveError: the source folder already holds a different manifest, a post file is
            invalid, or the eval files can't be rewritten safely. Nothing is deleted then.
    """
    source = config.source(source_id)
    out, result = config.output_dir, MigrateResult()
    old_manifest = config.old_manifest_path
    if not old_manifest.exists():
        result.nothing_to_move = True
        return result
    if source.manifest_path.exists() and not _same_bytes(old_manifest, source.manifest_path):
        raise ArchiveError(
            f"{source.manifest_path} already exists and differs from {old_manifest}; "
            "move one of them aside before migrating"
        )
    raws = sorted((out / "raw").glob("*.html"))
    posts = sorted((out / "posts").glob("*.json"))
    evals = out / EVAL_FILE
    judgments = out / JUDGE_FILE
    result.manifest, result.raw, result.posts = True, len(raws), len(posts)
    result.examples = {
        "pages": [f"raw/{p.name} -> {source.id}/raw/{p.name}" for p in raws[:EXAMPLES]],
        "posts": [f"posts/{p.name} -> {source.id}/posts/{p.name}" for p in posts[:EXAMPLES]],
    }
    new_evals = None
    if evals.exists():
        new_evals, result.eval_ids = prefix_eval_queries(evals.read_text(encoding="utf-8"), source.id)
    graded = load_judgments(judgments) if judgments.exists() else {}
    result.eval_ids += sum("/" not in p for j in graded.values() for p in j.grades)
    if dry_run:
        return result

    with run_lock(config.lock_path):
        # Write phase: every new file, old ones untouched.
        for path in raws:
            target = source.raw_path(path.stem)
            if not _same_bytes(path, target):
                target.parent.mkdir(parents=True, exist_ok=True)
                tmp = target.with_suffix(".html.tmp")
                shutil.copy2(path, tmp)
                tmp.replace(target)
        for path in posts:
            record = _v2_record(json.loads(path.read_text(encoding="utf-8")), source, path.stem)
            write_json_atomic(source.post_path(path.stem), record)
        if new_evals is not None:
            write_text_atomic(evals, new_evals)
        if graded:
            for j in graded.values():
                j.grades = {_prefixed(p, source.id): g for p, g in j.grades.items()}
            save_judgments(judgments, graded)
        write_text_atomic(source.manifest_path, old_manifest.read_text(encoding="utf-8"))

        # Commit phase: only once every new file is in place, delete the old ones, manifest last.
        missing = [p.name for p in raws if not source.raw_path(p.stem).exists()]
        missing += [p.name for p in posts if not source.post_path(p.stem).exists()]
        if missing:
            raise ArchiveError(
                f"not deleting anything: {len(missing)} new files are missing ({missing[0]}, ...)"
            )
        for path in [*raws, *posts]:
            path.unlink()
        for folder in (out / "raw", out / "posts"):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
        old_manifest.unlink()
    log.info("moved %d pages and %d posts into %s", len(raws), len(posts), source.root)
    return result


__all__ = ["MigrateResult", "migrate", "prefix_eval_queries"]

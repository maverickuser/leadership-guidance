"""Stage 4: safe file writes, the manifest, and per-post JSON records."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .errors import ArchiveError, ExtractError
from .extract import extract_post
from .models import Extracted, Manifest, ManifestEntry, PostCallback, iso_utc, no_callback, utc_now_iso
from .schema import SCHEMA_VERSION, validate_post

log = logging.getLogger("scarlet_archive.store")

MANIFEST_VERSION = 1


def write_text_atomic(path: Path, text: str) -> None:
    """Write text so readers see either the old file or the new one, never half of it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp name, so two writers never share a half-written file.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # mkstemp creates 0600; match a normal file
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def run_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on `path` for the whole run.

    Every stage rewrites manifest.json after each post, so two runs at once would
    overwrite each other's progress.

    Raises:
        ArchiveError: another run holds the lock.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - not on POSIX; run unlocked
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ArchiveError(f"another archive run is using {path.parent}; wait for it to finish") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def dumps_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def write_json_atomic(path: Path, obj: Any) -> None:
    write_text_atomic(path, dumps_json(obj))


def load_manifest(path: Path) -> Manifest:
    """Read manifest.json; an absent file is an empty manifest.

    Raises:
        ArchiveError: the file is not valid JSON or has a malformed entry.
    """
    path = Path(path)
    if not path.is_file():
        return Manifest()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ArchiveError(f"{path} is not valid JSON ({e}); fix or delete it") from None
    posts = data.get("posts", []) if isinstance(data, dict) else None
    if not isinstance(posts, list):
        raise ArchiveError(f"{path}: expected an object with a 'posts' list; fix or delete it")
    entries: dict[str, ManifestEntry] = {}
    for i, item in enumerate(posts):
        try:
            entry = ManifestEntry.from_dict(item)
        except ValueError as e:
            raise ArchiveError(f"{path}: post #{i + 1}: {e}") from None
        entries[entry.slug] = entry
    return Manifest(entries=entries)


def save_manifest(manifest: Manifest, path: Path) -> None:
    write_json_atomic(
        path,
        {
            "version": MANIFEST_VERSION,
            "updated_at": utc_now_iso(),
            "posts": [e.to_dict() for e in manifest.ordered()],
        },
    )


# -- post records (stage 4) ------------------------------------------------


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mtime_iso(path: Path) -> str:
    return iso_utc(datetime.fromtimestamp(path.stat().st_mtime, tz=UTC))


def build_record(entry: ManifestEntry, ex: Extracted, fetched_at: str, min_word_count: int) -> dict[str, Any]:
    """The JSON record for one post. It is `partial` if short or cut off by the paywall."""
    partial = ex.word_count < min_word_count or ex.paywalled
    return {
        "schema_version": SCHEMA_VERSION,
        "id": entry.slug,
        "url": entry.url,
        "title": ex.title,
        "subtitle": ex.subtitle,
        "author": ex.author,
        "published_date": ex.published_date,
        "categories": list(entry.categories),
        "word_count": ex.word_count,
        "summary": ex.summary,
        "sections": [asdict(s) for s in ex.sections],
        "text": ex.text,
        "links": ex.links,
        "content_hash": content_hash(ex.text),
        "fetched_at": fetched_at,
        "status": "partial" if partial else "ok",
    }


@dataclass
class ExtractResult:
    ok: int = 0
    partial: int = 0
    failed: int = 0
    written: int = 0
    unchanged: int = 0
    would_write: list[str] = field(default_factory=list)


def _stored_categories(path: Path) -> list[str] | None:
    try:
        cats = json.loads(path.read_text(encoding="utf-8")).get("categories")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return cats if isinstance(cats, list) else None


def run_extract(
    config: Config,
    manifest: Manifest,
    limit: int | None = None,
    refresh: bool = False,
    dry_run: bool = False,
    on_post: PostCallback = no_callback,
) -> ExtractResult:
    """Extract every cached post into posts/{slug}.json. Reads data/raw/ only; no network."""
    result = ExtractResult()
    candidates = manifest.ordered() if limit is None else manifest.ordered()[:limit]
    for entry in candidates:
        raw = config.raw_path(entry.slug)
        if not raw.exists():
            continue
        try:
            ex = extract_post(raw.read_text(encoding="utf-8"), entry.url)
            record = build_record(entry, ex, mtime_iso(raw), config.min_word_count)
            validate_post(record)
        except Exception as e:  # one bad page must not stop the run
            reason = str(e) if isinstance(e, ExtractError) else f"{type(e).__name__}: {e}"
            log.debug("extract failed for %s", entry.slug, exc_info=not isinstance(e, ExtractError))
            result.failed += 1
            if not dry_run:
                entry.status, entry.error = "failed", f"extract: {reason}"
                save_manifest(manifest, config.manifest_path)
            on_post(entry.slug, "extract", "failed", reason)
            continue

        post_path = config.post_path(entry.slug)
        unchanged = (
            not refresh
            and post_path.exists()
            and entry.content_hash == record["content_hash"]
            and _stored_categories(post_path) == record["categories"]
        )
        status = record["status"]
        if status == "partial":
            result.partial += 1
        else:
            result.ok += 1
        if dry_run:
            if not unchanged:
                result.would_write.append(entry.slug)
            on_post(entry.slug, "extract", "unchanged" if unchanged else "would-write", None)
            continue
        if unchanged:
            result.unchanged += 1
        else:
            write_json_atomic(post_path, record)
            result.written += 1
        entry.status, entry.error, entry.content_hash = status, None, record["content_hash"]
        save_manifest(manifest, config.manifest_path)
        on_post(entry.slug, "extract", status, "unchanged" if unchanged else None)
    return result

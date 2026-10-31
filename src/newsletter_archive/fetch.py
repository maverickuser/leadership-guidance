"""Stage 2: download post pages with the user's cookie into data/raw/."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .config import Config
from .errors import FetchError, MissingCookieError, NotSignedInError
from .http import Client
from .models import Manifest, ManifestEntry, PostCallback, no_callback, utc_now_iso
from .store import save_manifest, write_text_atomic

log = logging.getLogger("newsletter_archive.fetch")

# Present only on pages rendered for a signed-in member.
SIGNED_IN_MARKER = 'data-portal="account"'


@dataclass
class FetchResult:
    fetched: int = 0
    failed: int = 0
    skipped: int = 0
    would_fetch: list[str] = field(default_factory=list)


def fetch_url(entry: ManifestEntry, base_url: str | None = None) -> str:
    """Canonical form with a trailing slash, so Ghost doesn't answer with a redirect.

    With base_url, the request goes to base_url's scheme and host (e.g. a non-www sitemap
    link is fetched from www), which is the only origin the cookie is sent to."""
    parts = urlsplit(entry.url)
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    scheme, netloc = parts.scheme, parts.netloc
    if base_url:
        base = urlsplit(base_url)
        scheme, netloc = base.scheme, base.netloc
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def raw_path(config: Config, slug: str) -> Path:
    return config.raw_dir / f"{slug}.html"


def select_for_fetch(
    manifest: Manifest, config: Config, limit: int | None, refresh: bool
) -> tuple[list[ManifestEntry], list[ManifestEntry]]:
    """Return (to fetch, skipped). The limit picks the first N posts before skipping,
    so repeating `--limit 5` keeps touching the same 5 posts."""
    candidates = manifest.ordered() if limit is None else manifest.ordered()[:limit]
    if refresh:
        return candidates, []
    todo = [e for e in candidates if not raw_path(config, e.slug).exists()]
    skipped = [e for e in candidates if raw_path(config, e.slug).exists()]
    return todo, skipped


def run_fetch(
    client: Client,
    config: Config,
    manifest: Manifest,
    limit: int | None = None,
    refresh: bool = False,
    dry_run: bool = False,
    on_post: PostCallback = no_callback,
) -> FetchResult:
    if not config.cookie:
        raise MissingCookieError()
    result = FetchResult()
    todo, skipped = select_for_fetch(manifest, config, limit, refresh)
    result.skipped = len(skipped)
    for entry in skipped:
        on_post(entry.slug, "fetch", "skipped", None)

    if dry_run:
        for entry in todo:
            result.would_fetch.append(entry.slug)
            on_post(entry.slug, "fetch", "would-fetch", fetch_url(entry, config.base_url))
        return result

    for entry in todo:
        try:
            resp = client.get(fetch_url(entry, config.base_url))
            if resp.status != 200:
                raise FetchError(resp.url, f"HTTP {resp.status}", status=resp.status)
        except FetchError as e:
            entry.status, entry.error, entry.last_attempt = "failed", e.reason, utc_now_iso()
            save_manifest(manifest, config.manifest_path)
            result.failed += 1
            on_post(entry.slug, "fetch", "failed", e.reason)
            continue

        if SIGNED_IN_MARKER not in resp.text:
            # Don't cache a signed-out page and don't touch the entry: stop right here.
            save_manifest(manifest, config.manifest_path)
            on_post(entry.slug, "fetch", "not-signed-in", None)
            raise NotSignedInError(entry.slug)

        write_text_atomic(raw_path(config, entry.slug), resp.text)
        entry.status, entry.error, entry.last_attempt = "fetched", None, utc_now_iso()
        save_manifest(manifest, config.manifest_path)
        result.fetched += 1
        on_post(entry.slug, "fetch", "fetched", None)
    return result

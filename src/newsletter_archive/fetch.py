"""Stage 2: download post pages with the user's cookie into data/raw/."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from .config import Config, SourceConfig
from .errors import FetchError, MissingCookieError, NotSignedInError
from .http import Client, to_base_host
from .models import Manifest, ManifestEntry, PostCallback, no_callback, utc_now_iso
from .platforms import Platform, get_platform
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
    return fetch_url_for(entry.url, base_url)


def fetch_url_for(url: str, base_url: str | None = None) -> str:
    """`fetch_url` for a bare URL: the Ghost platform's request URL."""
    url = to_base_host(url, base_url) if base_url else url
    parts = urlsplit(url)
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def select_for_fetch(
    manifest: Manifest, source: SourceConfig, limit: int | None, refresh: bool
) -> tuple[list[ManifestEntry], list[ManifestEntry]]:
    """Return (to fetch, skipped). The limit picks the first N posts before skipping,
    so repeating `--limit 5` keeps touching the same 5 posts."""
    candidates = manifest.ordered() if limit is None else manifest.ordered()[:limit]
    if refresh:
        return candidates, []
    todo = [e for e in candidates if not source.raw_path(e.slug).exists()]
    skipped = [e for e in candidates if source.raw_path(e.slug).exists()]
    return todo, skipped


def run_fetch(
    client: Client,
    config: Config,
    manifest: Manifest,
    limit: int | None = None,
    refresh: bool = False,
    dry_run: bool = False,
    on_post: PostCallback = no_callback,
    platform: Platform | None = None,
    source: SourceConfig | None = None,
) -> FetchResult:
    """Download each selected post's page into raw/, saving progress after every post.

    source defaults to the only configured one; the platform (default: the source's) decides
    the URL to request and how a signed-in page looks.

    Raises:
        MissingCookieError: no cookie is configured.
        NotSignedInError: a page came back without the signed-in marker (cookie expired).
    """
    source = source or config.only_source()
    # The client knows its own site and cookie; a dry run may have no client.
    base_url = client.base_url if client is not None else source.base_url
    if not (client.has_cookie if client is not None else source.cookie):
        raise MissingCookieError(source.display_name, source.cookie_env)
    platform = platform or get_platform(source.platform)
    result = FetchResult()
    todo, skipped = select_for_fetch(manifest, source, limit, refresh)
    result.skipped = len(skipped)
    for entry in skipped:
        on_post(entry.slug, "fetch", "skipped", None)

    if dry_run:
        for entry in todo:
            result.would_fetch.append(entry.slug)
            on_post(entry.slug, "fetch", "would-fetch", platform.request_url(entry.url, base_url))
        return result

    for entry in todo:
        try:
            resp = client.get(platform.request_url(entry.url, base_url))
            if resp.status != 200:
                raise FetchError(resp.url, f"HTTP {resp.status}", status=resp.status)
        except FetchError as e:
            entry.status, entry.error, entry.last_attempt = "failed", e.reason, utc_now_iso()
            save_manifest(manifest, source.manifest_path)
            result.failed += 1
            on_post(entry.slug, "fetch", "failed", e.reason)
            continue

        if not platform.signed_in(resp.text):
            # Don't cache a signed-out page and don't touch the entry: stop right here.
            save_manifest(manifest, source.manifest_path)
            on_post(entry.slug, "fetch", "not-signed-in", None)
            raise NotSignedInError(entry.slug, source.display_name, source.cookie_env)

        write_text_atomic(source.raw_path(entry.slug), resp.text)
        entry.status, entry.error, entry.last_attempt = "fetched", None, utc_now_iso()
        save_manifest(manifest, source.manifest_path)
        result.fetched += 1
        on_post(entry.slug, "fetch", "fetched", None)
    return result

"""Stage 1: find every post URL (sitemap) and its categories (category listings)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from lxml import etree

from .config import Config
from .errors import ArchiveError, FetchError
from .http import Client
from .models import Manifest, ManifestEntry
from .store import save_manifest

log = logging.getLogger("newsletter_archive.discover")

TRACKING_PARAMS = {"ref", "source", "fbclid", "gclid", "mc_cid", "mc_eid"}
EXCLUDED_SEGMENTS = {
    "tag", "t", "author", "categories", "page", "p", "rss", "about", "contact",
    "subscribe", "signin", "signup", "account", "archive", "membership",
}
GHOST_POSTS_SITEMAP = re.compile(r"/sitemap-posts(-\d+)?\.xml$")
CATEGORY_PATH = re.compile(r"^/t/([^/]+)/?$")
MAX_TAG_PAGES = 200


# -- URLs ------------------------------------------------------------------


def _site_host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def same_site(url: str, base_url: str) -> bool:
    return _site_host(url) == _site_host(base_url)


def normalize_url(url: str) -> str:
    """Lowercase scheme/host, drop tracking params and fragment, strip trailing slash."""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.lower().startswith("utm_") or k.lower() in TRACKING_PARAMS)
    ]
    netloc = (parts.hostname or "").lower()
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), urlencode(sorted(query)), ""))


def is_post_url(url: str, base_url: str) -> bool:
    if not same_site(url, base_url):
        return False
    segments = [s for s in urlsplit(url).path.split("/") if s]
    return len(segments) == 1 and segments[0].lower() not in EXCLUDED_SEGMENTS


def slug_from_url(url: str) -> str:
    segments = [s for s in urlsplit(url).path.split("/") if s]
    if not segments:
        raise ValueError(f"no slug in {url}")
    slug = re.sub(r"[^a-z0-9-]+", "-", unquote(segments[-1]).lower()).strip("-")
    if not slug:
        raise ValueError(f"no usable slug in {url}")
    return slug


# -- sitemap ---------------------------------------------------------------


def _local(tag) -> str:
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _parse_sitemap(text: str, url: str) -> tuple[str, list[str]]:
    try:
        root = etree.fromstring(text.encode("utf-8"), parser=etree.XMLParser(resolve_entities=False))
    except etree.XMLSyntaxError as e:
        raise ArchiveError(f"could not parse sitemap {url}: {e}") from None
    kind = _local(root.tag)
    locs = [
        el.text.strip()
        for el in root.iter()
        if _local(el.tag) == "loc" and el.text and _local(el.getparent().tag) in ("url", "sitemap")
    ]
    return kind, locs


def collect_sitemap_urls(client: Client, sitemap_url: str) -> list[str]:
    """All page URLs reachable from a sitemap, following nested indexes.

    For a Ghost index (it lists sitemap-posts.xml), only the posts sitemaps are read.
    """
    urls: list[str] = []
    visited: set[str] = set()

    def visit(url: str) -> None:
        if url in visited:
            return
        visited.add(url)
        kind, locs = _parse_sitemap(client.get(url).text, url)
        if kind == "sitemapindex":
            posts = [loc for loc in locs if GHOST_POSTS_SITEMAP.search(urlsplit(loc).path)]
            for child in posts or locs:
                visit(child)
        elif kind == "urlset":
            urls.extend(locs)
        else:
            log.warning("ignoring %s: not a sitemap (root <%s>)", url, kind)

    visit(sitemap_url)
    return urls


def post_urls_from_sitemap(urls: list[str], base_url: str) -> list[str]:
    seen: dict[str, None] = {}
    for u in urls:
        n = normalize_url(u)
        if is_post_url(n, base_url):
            seen.setdefault(n, None)
    return list(seen)


# -- categories ------------------------------------------------------------


@dataclass
class CategoryCrawl:
    post_categories: dict[str, list[str]] = field(default_factory=dict)  # post url -> sorted names
    names: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # category slugs that could not be crawled


def category_slugs(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    slugs: list[str] = []
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url + "/", a["href"])
        m = CATEGORY_PATH.match(urlsplit(url).path)
        if m and same_site(url, base_url) and m.group(1) not in slugs:
            slugs.append(m.group(1))
    return slugs


def _title_name(soup: BeautifulSoup) -> str | None:
    if soup.title is None or not soup.title.get_text(strip=True):
        return None
    title = " ".join(soup.title.get_text().split())
    return title.rsplit(" - ", 1)[0].strip() if " - " in title else title


def crawl_category(client: Client, base_url: str, slug: str) -> tuple[str, list[str]]:
    """Return (category name, post URLs) for one category, following rel=next pages."""
    url = f"{base_url}/t/{slug}"
    seen_pages: set[str] = set()
    found: dict[str, None] = {}
    name: str | None = None
    pages = 0
    while url and url not in seen_pages and pages < MAX_TAG_PAGES:
        seen_pages.add(url)
        resp = client.get(url)
        seen_pages.add(resp.url)
        pages += 1
        soup = BeautifulSoup(resp.text, "lxml")
        if name is None:
            name = _title_name(soup) or slug
        new = 0
        for a in soup.select("article.gh-card a.gh-card-link[href]"):
            post = normalize_url(urljoin(resp.url, a["href"]))
            if is_post_url(post, base_url) and post not in found:
                found[post] = None
                new += 1
        if new == 0:
            break
        nxt = soup.find("link", rel="next")
        href = nxt.get("href") if nxt else None
        url = urljoin(resp.url, href) if href else None
        if url and not same_site(url, base_url):
            break
    log.info("category %s: %d posts on %d page(s)", name, len(found), pages)
    return name or slug, list(found)


def crawl_categories(client: Client, base_url: str) -> CategoryCrawl:
    result = CategoryCrawl()
    slugs = category_slugs(client.get(f"{base_url}/categories/").text, base_url)
    log.info("found %d categories", len(slugs))
    mapping: dict[str, set[str]] = {}
    for slug in slugs:
        try:
            name, posts = crawl_category(client, base_url, slug)
        except FetchError as e:
            log.warning("category %s could not be crawled: %s", slug, e.reason)
            result.failed.append(slug)
            continue
        result.names.append(name)
        for post in posts:
            mapping.setdefault(post, set()).add(name)
    result.post_categories = {post: sorted(names) for post, names in mapping.items()}
    return result


# -- manifest --------------------------------------------------------------


@dataclass
class MergeStats:
    added: int = 0
    updated: int = 0
    stale: int = 0
    skipped: int = 0  # new posts left out because of --limit


def merge_manifest(
    manifest: Manifest,
    post_urls: list[str],
    post_categories: dict[str, list[str]],
    limit: int | None = None,
) -> MergeStats:
    """Add new posts, refresh categories, keep progress. Order follows the sitemap."""
    stats = MergeStats()
    by_url = {e.url: e for e in manifest.ordered()}
    merged: dict[str, ManifestEntry] = {}
    for url in post_urls:
        cats = list(post_categories.get(url, []))
        existing = by_url.get(url)
        if existing is not None:
            existing.categories = cats
            merged[existing.slug] = existing
            stats.updated += 1
            continue
        if limit is not None and stats.added >= limit:
            stats.skipped += 1
            continue
        base = slug = slug_from_url(url)
        n = 2
        while slug in merged or (slug in manifest.entries and manifest.entries[slug].url != url):
            slug = f"{base}-{n}"
            n += 1
        if slug != base:
            log.warning("slug %s already used; storing %s as %s", base, url, slug)
        merged[slug] = ManifestEntry(url=url, slug=slug, categories=cats)
        stats.added += 1
    for entry in manifest.ordered():
        if entry.slug not in merged:
            merged[entry.slug] = entry
            stats.stale += 1
            log.info("%s is no longer in the sitemap; kept", entry.slug)
    manifest.entries = merged
    return stats


@dataclass
class DiscoverResult:
    sitemap_posts: int
    stats: MergeStats
    categories: int
    failed_categories: list[str]
    total: int


def run_discover(
    client: Client,
    config: Config,
    manifest: Manifest,
    limit: int | None = None,
    dry_run: bool = False,
) -> DiscoverResult:
    base = config.base_url
    post_urls = post_urls_from_sitemap(collect_sitemap_urls(client, f"{base}/sitemap.xml"), base)
    log.info("sitemap: %d posts", len(post_urls))
    crawl = crawl_categories(client, base)
    extra = set(crawl.post_categories) - set(post_urls)
    if extra:
        log.debug("%d posts in category listings but not in the sitemap (ignored)", len(extra))

    # In a dry run the merge happens in memory only, so `run --dry-run` can list new posts.
    stats = merge_manifest(manifest, post_urls, crawl.post_categories, limit=limit)
    if dry_run:
        log.info("dry run: would add %d, update %d; manifest not written", stats.added, stats.updated)
    else:
        save_manifest(manifest, config.manifest_path)
    if crawl.failed:
        log.warning("categories incomplete: %s", ", ".join(crawl.failed))
    return DiscoverResult(
        sitemap_posts=len(post_urls),
        stats=stats,
        categories=len(crawl.names),
        failed_categories=crawl.failed,
        total=len(manifest),
    )

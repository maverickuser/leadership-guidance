"""Ghost newsletters: the rules the archive has always used, behind the Platform interface."""

from __future__ import annotations

from ..discover import CategoryCrawl, crawl_categories, post_urls_from_sitemap
from ..extract import extract_post
from ..fetch import SIGNED_IN_MARKER, fetch_url_for
from ..http import Client
from ..models import Extracted


class GhostPlatform:
    """Posts are single-segment paths; categories come from the `/categories/` tag pages;
    a signed-in page carries `data-portal="account"`; the body is `.gh-content`."""

    name = "ghost"

    def post_urls(self, sitemap_urls: list[str], base_url: str) -> list[str]:
        return post_urls_from_sitemap(sitemap_urls, base_url)

    def discover_categories(self, client: Client, base_url: str) -> CategoryCrawl | None:
        return crawl_categories(client, base_url)

    def request_url(self, url: str, base_url: str) -> str:
        return fetch_url_for(url, base_url)

    def signed_in(self, html: str) -> bool:
        return SIGNED_IN_MARKER in html

    def extract(
        self, html: str, page_url: str, boilerplate_prefixes: tuple[str, ...], min_word_count: int
    ) -> Extracted:
        # Ghost's paywall box is detected in the body; the word-count rule is applied when the
        # record is built, so min_word_count isn't needed here.
        return extract_post(html, page_url, boilerplate_prefixes)

"""Newsletter platforms: what differs between Ghost, Substack and others.

Each platform says which sitemap URLs are posts, how categories are found, which URL to
request, how to tell a signed-in page, and how to turn a saved page into clean content.
Everything else (the sitemap walker, the HTTP client, storage) is shared.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Protocol

from ..errors import ConfigError

if TYPE_CHECKING:
    from ..discover import CategoryCrawl
    from ..http import Client
    from ..models import Extracted


class Platform(Protocol):
    """The platform-specific steps of discover, fetch and extract."""

    name: str

    def post_urls(self, sitemap_urls: list[str], base_url: str) -> list[str]:
        """The normalized, deduplicated post URLs among a sitemap's URLs, in sitemap order."""
        ...

    def discover_categories(self, client: Client, base_url: str) -> CategoryCrawl | None:
        """Crawl the site's category listings, or None when categories come from post pages."""
        ...

    def request_url(self, url: str, base_url: str) -> str:
        """The URL to request for a post, so the site answers without a redirect."""
        ...

    def signed_in(self, html: str) -> bool:
        """Whether a fetched post page was rendered for a signed-in reader."""
        ...

    def extract(
        self, html: str, page_url: str, boilerplate_prefixes: tuple[str, ...], min_word_count: int
    ) -> Extracted:
        """Clean content and metadata from a saved post page.

        Raises:
            ExtractError: the page has no usable article body or metadata.
        """
        ...


# Every platform name, so config validation can check a name without loading the platforms.
PLATFORM_NAMES: tuple[str, ...] = ("ghost", "substack")


@cache
def get_platform(name: str) -> Platform:
    """The platform registered under `name` (one shared instance per name).

    Raises:
        ConfigError: no such platform; the message lists the valid names.
    """
    if name not in PLATFORM_NAMES:
        raise ConfigError(f"unknown platform {name!r}; use one of: {', '.join(PLATFORM_NAMES)}")
    # Imported here, not at the top: the platforms import the stage modules.
    if name == "substack":
        from .substack import SubstackPlatform

        return SubstackPlatform()
    from .ghost import GhostPlatform

    return GhostPlatform()


__all__ = ["PLATFORM_NAMES", "Platform", "get_platform"]

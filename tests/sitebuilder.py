"""Build fake Ghost pages from the structure-only fixtures."""

from __future__ import annotations

from bs4 import BeautifulSoup

from .conftest import fixture_text


def tag_page(title: str, slugs: list[str], next_href: str | None = None) -> str:
    """A tag listing with the real Ghost card markup, listing the given post slugs."""
    soup = BeautifulSoup(fixture_text("tag-career-growth-p1.html"), "lxml")
    soup.title.string = f"{title} - Example Newsletter"
    for link in soup.find_all("link", rel="next"):
        link.decompose()
    if next_href:
        soup.head.append(soup.new_tag("link", rel="next", href=next_href))
    cards = soup.select("article.gh-card")
    template = cards[0]
    container = template.parent
    for card in cards:
        card.extract()
    for slug in slugs:
        card = BeautifulSoup(str(template), "lxml").select_one("article.gh-card")
        card.select_one("a.gh-card-link")["href"] = f"/{slug}/"
        container.append(card)
    return str(soup)


def sitemap_urlset(base: str, slugs: list[str]) -> str:
    urls = "".join(f"<url><loc>{base}/{s}/</loc></url>" for s in slugs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    )


def sitemap_index(base: str, names: list[str]) -> str:
    items = "".join(f"<sitemap><loc>{base}/{n}</loc></sitemap>" for n in names)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</sitemapindex>'
    )


def categories_page(base: str, slugs: list[str]) -> str:
    links = "".join(f'<li><a href="{base}/t/{s}">{s}</a></li>' for s in slugs)
    return f"<html><head><title>Categories</title></head><body><ul>{links}</ul></body></html>"


def post_page(slug: str, signed_in: bool = True) -> str:
    """A post page from the signed-in or signed-out fixture, with og:url set to this slug."""
    html = fixture_text("post-signed-in.html" if signed_in else "post-signed-out.html")
    return html.replace(
        "stop-trying-harder-change-your-environment-instead", slug
    )

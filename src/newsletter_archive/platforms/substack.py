"""Substack newsletters (including custom domains such as newsletter.pragmaticengineer.com).

Measured on a saved Pragmatic Engineer post (2026-11-11, signed in with the user's cookie):

- The page data is plain JSON in `<script type="application/json" id="preloads-json">`.
  Older pages put it in `window._preloads = JSON.parse("...")`; both are read.
- Signed in: `user` in that data is an object with an `id`. Signed out: `user` is null.
- A paid post read without access has a `div.paywall` block after the body. The signed-in
  page has only a `div.paywall-jump` anchor inside the body, which is not a paywall.
- The article body is `div.available-content div.body.markup`.
- Categories are Substack sections: `post.section_id` names one of `pub.sections`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from ..discover import normalize_url, same_site
from ..errors import ExtractError
from ..extract import (
    _json_ld_article,
    _ld_author,
    _meta,
    _text_of,
    build_sections,
    clean_body,
    extract_links,
    first_sentences,
    to_iso_date,
)
from ..http import Client, to_base_host
from ..models import Extracted

log = logging.getLogger("newsletter_archive.platforms.substack")

POST_PATH = re.compile(r"^/p/[^/]+$")
BODY_SELECTOR = "div.available-content div.body.markup"
PRELOADS_SCRIPT_ID = "preloads-json"
LEGACY_PRELOADS = re.compile(r"window\._preloads\s*=\s*JSON\.parse\(\s*(\"(?:[^\"\\]|\\.)*\")\s*\)", re.S)

# Clutter inside the body: images and their captions and buttons, heading link buttons,
# embeds, subscribe/share widgets, and the paywall jump anchor. Each Substack class here was
# seen on a real page or is a documented Substack widget; tests assert each one is removed.
REMOVE_SELECTORS = ", ".join(
    [
        "img",
        "picture",
        "source",
        "figure",
        "figcaption",
        "svg",
        "video",
        "audio",
        "iframe",
        "object",
        "embed",
        "script",
        "style",
        "noscript",
        "form",
        "button",
        "input",
        ".captioned-image-container",
        ".image-link-expand",
        ".image-gallery-embed",
        ".header-anchor-parent",
        ".youtube-wrap",
        ".native-video-embed",
        ".tweet",
        ".embedded-post-wrap",
        ".digest-post-embed",
        ".subscription-widget-wrap",
        ".subscription-widget-wrap-editor",
        ".captioned-button-wrap",
        ".button-wrapper",
        ".paywall-jump",
        ".poll-embed",
        '[class*="share"]',
        '[class*="subscribe"]',
    ]
)


def decode_legacy_preloads(literal: str) -> dict[str, Any]:
    """The data in `window._preloads = JSON.parse("...")`: a JSON string literal holding JSON.

    Raises:
        ExtractError: the literal or the JSON inside it is broken.
    """
    try:
        inner = json.loads(literal)
        data = json.loads(inner) if isinstance(inner, str) else None
    except (json.JSONDecodeError, TypeError):
        data = None
    if not isinstance(data, dict):
        raise ExtractError("no Substack page data")
    return data


def preloads(soup: BeautifulSoup) -> dict[str, Any]:
    """The page data Substack renders the post from.

    Raises:
        ExtractError: no page data, or it can't be read.
    """
    script = soup.find("script", id=PRELOADS_SCRIPT_ID)
    if isinstance(script, Tag):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            return data
        raise ExtractError("no Substack page data")
    for s in soup.find_all("script"):
        m = LEGACY_PRELOADS.search(s.string or "")
        if m:
            return decode_legacy_preloads(m.group(1))
    raise ExtractError("no Substack page data")


def _post(data: dict[str, Any]) -> dict[str, Any]:
    post = data.get("post")
    return post if isinstance(post, dict) else {}


def section_categories(data: dict[str, Any]) -> list[str]:
    """[the post's section name], or [] when it has none (or the section isn't listed)."""
    section_id = _post(data).get("section_id")
    pub = data.get("pub")
    sections = pub.get("sections") if isinstance(pub, dict) else None
    if section_id is None or not isinstance(sections, list):
        return []
    for s in sections:
        if isinstance(s, dict) and s.get("id") == section_id and isinstance(s.get("name"), str):
            name = s["name"].strip()
            return [name] if name else []
    return []


class SubstackPlatform:
    """Posts are `/p/<slug>`; categories are sections, read from each post page at extraction;
    a signed-in page has a `user` object in its page data; the body is `div.body.markup`."""

    name = "substack"

    def post_urls(self, sitemap_urls: list[str], base_url: str) -> list[str]:
        seen: dict[str, None] = {}
        for u in sitemap_urls:
            n = normalize_url(u)
            if same_site(n, base_url) and POST_PATH.match(urlsplit(n).path):
                seen.setdefault(n, None)
        return list(seen)

    def discover_categories(self, client: Client, base_url: str) -> None:
        return None  # sections come from each post page; no extra requests

    def request_url(self, url: str, base_url: str) -> str:
        return to_base_host(url, base_url)  # measured: 200 with no redirect

    def signed_in(self, html: str) -> bool:
        try:
            data = preloads(BeautifulSoup(html, "lxml"))
        except ExtractError:
            return False
        user = data.get("user")
        return isinstance(user, dict) and user.get("id") is not None

    def extract(
        self, html: str, page_url: str, boilerplate_prefixes: tuple[str, ...], min_word_count: int
    ) -> Extracted:
        soup = BeautifulSoup(html, "lxml")
        data = preloads(soup)
        post = _post(data)
        body = soup.select_one(BODY_SELECTOR)
        if body is None:
            raise ExtractError("no article body found")
        cleaned = clean_body(body, boilerplate_prefixes, REMOVE_SELECTORS)
        sections = build_sections(cleaned, page_url)
        text = "\n\n".join(s.text for s in sections)
        word_count = len(text.split())
        paywalled = soup.select_one("div.paywall") is not None or (
            post.get("audience") == "only_paid" and word_count < min_word_count
        )

        ld = _json_ld_article(soup)
        title = (
            _meta(soup, prop="og:title")
            or (str(ld["headline"]).strip() if ld.get("headline") else None)
            or _text_of(soup, "h1.post-title")
            or (str(post["title"]).strip() if post.get("title") else None)
        )
        if not title:
            raise ExtractError("no title found")
        author = _ld_author(ld) or _meta(soup, name="author")
        if not author:
            log.warning("%s: no author found; using 'Unknown'", page_url)
            author = "Unknown"
        published = to_iso_date(
            ld.get("datePublished") if isinstance(ld.get("datePublished"), str) else None
        ) or to_iso_date(post.get("post_date") if isinstance(post.get("post_date"), str) else None)
        if not published:
            raise ExtractError("no published date found")
        summary = (
            _meta(soup, name="description") or _meta(soup, prop="og:description") or first_sentences(text)
        )
        return Extracted(
            title=title,
            subtitle=_text_of(soup, "h3.subtitle"),
            author=author,
            published_date=published,
            summary=summary,
            sections=sections,
            text=text,
            links=extract_links(cleaned, page_url),
            word_count=word_count,
            paywalled=paywalled,
            categories=section_categories(data),
        )


__all__ = ["SubstackPlatform", "decode_legacy_preloads", "preloads", "section_categories"]

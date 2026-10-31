"""Stage 3: turn a saved post page into clean, structured content.

Pure functions only: no files, no network.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import trafilatura
from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString
from lxml import etree

from .errors import ExtractError
from .models import Extracted, Section
from .parsing import attr, parse_xml

log = logging.getLogger("newsletter_archive.extract")

BODY_SELECTORS = ("article.gh-article section.gh-content", "article.gh-article .gh-content")

REMOVE_SELECTORS = ", ".join(
    [
        "img",
        "picture",
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
        ".gh-post-upgrade-cta",
        "[data-portal]",
        "[data-members-form]",
        ".kg-image-card",
        ".kg-gallery-card",
        ".kg-embed-card",
        ".kg-video-card",
        ".kg-audio-card",
        ".kg-file-card",
        ".kg-bookmark-card",
        ".kg-signup-card",
        ".kg-cta-card",
        ".kg-button-card",
        ".kg-product-card",
        ".kg-header-card",
        '[class*="share"]',
        '[class*="subscribe"]',
        '[class*="related"]',
        '[class*="comment"]',
        ".gh-comments",
        "#ghost-comments-root",
    ]
)

# The standard intro at the top of almost every post. Matched on exact openings, so a
# post's own italic note (e.g. "I'm writing about something different today!") stays.
BOILERPLATE_PREFIXES = ("Welcome to my newsletter!", "Free members can read")
BOILERPLATE_SCAN = 3

HEADING_RE = re.compile(r"^h([1-6])$")
BLOCK_TAGS = [
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "blockquote",
    "table",
    "pre",
    "div",
    "section",
]
ARTICLE_TYPES = {"Article", "BlogPosting", "NewsArticle"}
SITE_SUFFIX = re.compile(r"\s+-\s+Example Newsletter$")
TRAFILATURA_MIN_RATIO = 0.8
SUMMARY_MAX_CHARS = 400


def _squash(text: str) -> str:
    return " ".join(text.split())


# -- body cleaning ---------------------------------------------------------


def find_body(soup: BeautifulSoup) -> Tag | None:
    for sel in BODY_SELECTORS:
        body = soup.select_one(sel)
        if body is not None:
            return body
    return None


def strip_boilerplate(body: Tag) -> int:
    """Remove the standard intro paragraphs at the very top. Returns how many were removed."""
    removed = 0
    for el in [c for c in body.children if isinstance(c, Tag)][:BOILERPLATE_SCAN]:
        if el.name == "p" and _squash(el.get_text()).startswith(BOILERPLATE_PREFIXES):
            el.decompose()
            removed += 1
        else:
            break
    return removed


def clean_body(body: Tag) -> Tag:
    """Return a cleaned copy of the article body (the input is not modified)."""
    copy = BeautifulSoup(str(body), "lxml").find(body.name)
    if not isinstance(copy, Tag):  # pragma: no cover - re-parsing a tag always finds it
        raise ExtractError("could not copy the article body")
    for el in copy.select(REMOVE_SELECTORS):
        el.decompose()
    for el in copy.find_all(True):
        for name in ("alt", "title"):
            el.attrs.pop(name, None)
    strip_boilerplate(copy)
    return copy


# -- sections --------------------------------------------------------------


class _SectionBuilder:
    def __init__(self) -> None:
        self.sections: list[tuple[str | None, int, list[str]]] = [(None, 0, [])]

    def heading(self, text: str, level: int) -> None:
        text = _squash(text)
        if text:
            self.sections.append((text, level, []))

    def block(self, text: str) -> None:
        text = text.strip()
        if text:
            self.sections[-1][2].append(text)

    def build(self) -> list[Section]:
        return [Section(h, lvl, "\n\n".join(blocks)) for h, lvl, blocks in self.sections if blocks]


def _xml_text(el: Any) -> str:
    return _squash("".join(str(t) for t in el.itertext()))


def sections_from_trafilatura(xml: str | None) -> list[Section] | None:
    if not xml:
        return None
    try:
        root = parse_xml(xml)
    except etree.XMLSyntaxError:
        return None
    main = root.find("main")
    if main is None:
        return None
    b = _SectionBuilder()
    for el in main:
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag == "head":
            m = re.match(r"h([1-6])", el.get("rend", ""))
            b.heading(_xml_text(el), int(m.group(1)) if m else 2)
        elif tag == "list":
            b.block("\n".join(f"- {_xml_text(item)}" for item in el.findall("item") if _xml_text(item)))
        elif tag == "table":
            rows = [" | ".join(_xml_text(c) for c in row.findall("cell")) for row in el.findall("row")]
            b.block("\n".join(r for r in rows if r.strip(" |")))
        elif tag == "code":
            b.block("".join(str(t) for t in el.itertext()))
        elif tag:
            b.block(_xml_text(el))
    return b.build()


def sections_from_soup(body: Tag) -> list[Section]:
    """Fallback: walk the cleaned body directly."""
    b = _SectionBuilder()

    def walk(node: Tag) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                if child.strip():
                    b.block(_squash(child))
                continue
            if not isinstance(child, Tag):
                continue
            m = HEADING_RE.match(child.name)
            if m:
                b.heading(child.get_text(), int(m.group(1)))
            elif child.name in ("ul", "ol"):
                items = [_squash(li.get_text(" ")) for li in child.find_all("li", recursive=False)]
                b.block("\n".join(f"- {i}" for i in items if i))
            elif child.name == "table":
                rows = [
                    " | ".join(_squash(c.get_text(" ")) for c in tr.find_all(["th", "td"]))
                    for tr in child.find_all("tr")
                ]
                b.block("\n".join(r for r in rows if r.strip(" |")))
            elif child.name == "pre":
                b.block(child.get_text())
            elif child.name in ("p", "blockquote"):
                b.block(_squash(child.get_text(" ")))
            elif child.find(BLOCK_TAGS):
                walk(child)
            else:
                b.block(_squash(child.get_text(" ")))

    walk(body)
    return b.build()


def _word_count(sections: list[Section]) -> int:
    return sum(len(s.text.split()) + len((s.heading or "").split()) for s in sections)


def _trafilatura_xml(html: str) -> str | None:
    return trafilatura.extract(
        html,
        output_format="xml",
        include_comments=False,
        include_images=False,
        include_tables=True,
        include_links=False,
        include_formatting=False,
        favor_recall=True,
        deduplicate=False,
    )


def build_sections(cleaned: Tag, label: str = "") -> list[Section]:
    """trafilatura first; fall back to the soup walker if it lost headings or words."""
    ref_words = len(cleaned.get_text(" ").split())
    soup_has_headings = cleaned.find(["h2", "h3", "h4", "h5", "h6"]) is not None
    sections = sections_from_trafilatura(
        _trafilatura_xml(f"<html><body><article>{cleaned}</article></body></html>")
    )
    reason = None
    if sections is None:
        reason = "no output"
    elif ref_words and _word_count(sections) < TRAFILATURA_MIN_RATIO * ref_words:
        reason = f"kept {_word_count(sections)} of {ref_words} words"
    elif soup_has_headings and not any(s.heading for s in sections):
        reason = "lost the headings"
    if reason:
        log.warning("%s: trafilatura %s; using the plain HTML walker", label or "post", reason)
        return sections_from_soup(cleaned)
    assert sections is not None
    return sections


# -- links -----------------------------------------------------------------


def extract_links(cleaned: Tag, page_url: str) -> list[dict[str, str]]:
    base = page_url if page_url.endswith("/") else page_url + "/"
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for a in cleaned.find_all("a", href=True):
        href = (attr(a, "href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
            continue
        url = urljoin(base, href)
        if not url.startswith(("http://", "https://")):
            continue
        text = _squash(a.get_text(" "))
        if not text or (text, url) in seen:
            continue
        seen.add((text, url))
        out.append({"text": text, "url": url})
    return out


# -- metadata --------------------------------------------------------------


def _meta(soup: BeautifulSoup, *, prop: str | None = None, name: str | None = None) -> str | None:
    key, wanted = ("property", prop) if prop else ("name", name)
    value = attr(soup.find("meta", attrs={key: wanted or ""}), "content")
    return _squash(value) if value and value.strip() else None


def _json_ld_article(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = (
            data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        )
        for item in candidates:
            if not isinstance(item, dict):
                continue
            types = item.get("@type")
            types = set(types) if isinstance(types, list) else {types}
            if types & ARTICLE_TYPES:
                return item
    return {}


def _ld_author(ld: dict[str, Any]) -> str | None:
    author = ld.get("author")
    items = author if isinstance(author, list) else [author]
    names = []
    for a in items:
        if isinstance(a, dict) and a.get("name"):
            names.append(_squash(str(a["name"])))
        elif isinstance(a, str) and a.strip():
            names.append(_squash(a))
    return ", ".join(names) or None


def _text_of(soup: BeautifulSoup, selector: str) -> str | None:
    el = soup.select_one(selector)
    text = _squash(el.get_text(" ")) if el else ""
    return text or None


def to_iso_date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", value)
        return m.group(1) if m else None
    if dt.tzinfo is None:
        return dt.date().isoformat()
    return dt.astimezone(UTC).date().isoformat()


def first_sentences(text: str, n: int = 2) -> str:
    flat = _squash(text)
    parts = re.split(r"(?<=[.!?])\s+", flat)
    summary = " ".join(parts[:n]).strip()
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0] + "…"
    return summary


def extract_metadata(soup: BeautifulSoup, text: str) -> dict[str, Any]:
    ld = _json_ld_article(soup)
    title_tag = _squash(soup.title.get_text()) if soup.title else ""
    title = (
        _meta(soup, prop="og:title")
        or (_squash(str(ld["headline"])) if ld.get("headline") else None)
        or _text_of(soup, "h1.gh-article-title")
        or _text_of(soup, "h1")
        or (SITE_SUFFIX.sub("", title_tag) or None)
    )
    if not title:
        raise ExtractError("no title found")

    author = (
        _ld_author(ld)
        or _meta(soup, name="author")
        or _text_of(soup, ".gh-article-author-name")
        or _text_of(soup, "[rel=author]")
    )
    if not author:
        log.warning("no author found; using 'Unknown'")
        author = "Unknown"

    date_el = soup.find("time", attrs={"datetime": True})
    published = (
        to_iso_date(_meta(soup, prop="article:published_time"))
        or to_iso_date(ld.get("datePublished") if isinstance(ld.get("datePublished"), str) else None)
        or to_iso_date(attr(date_el, "datetime"))
    )
    if not published:
        raise ExtractError("no published date found")

    summary = _meta(soup, name="description") or _meta(soup, prop="og:description") or first_sentences(text)
    return {
        "title": title,
        "subtitle": _text_of(soup, "p.gh-article-excerpt"),
        "author": author,
        "published_date": published,
        "summary": summary,
    }


# -- entry point -----------------------------------------------------------


def extract_post(html: str, page_url: str) -> Extracted:
    """Turn one signed-in post page into clean sections, links and metadata.

    Raises:
        ExtractError: no article body or no usable publish date.
    """
    soup = BeautifulSoup(html, "lxml")
    body = find_body(soup)
    paywalled = False
    if body is not None:
        paywalled = body.select_one(".gh-post-upgrade-cta") is not None
        cleaned = clean_body(body)
        sections = build_sections(cleaned, page_url)
        links = extract_links(cleaned, page_url)
    else:
        log.warning("%s: no .gh-content found; extracting from the whole page", page_url)
        found = sections_from_trafilatura(_trafilatura_xml(html))
        if not found:
            raise ExtractError("no article body found")
        sections, links = found, []

    text = "\n\n".join(s.text for s in sections)
    meta = extract_metadata(soup, text)
    return Extracted(
        title=meta["title"],
        subtitle=meta["subtitle"],
        author=meta["author"],
        published_date=meta["published_date"],
        summary=meta["summary"],
        sections=sections,
        text=text,
        links=links,
        word_count=len(text.split()),
        paywalled=paywalled,
    )

#!/usr/bin/env python3
"""Build structure-only test fixtures from pages saved in data/inspect/.

The repo is public, so fixtures keep the real Ghost markup (tags, classes,
meta tags, heading structure, word counts) but never the article prose:
every text node inside the article body is replaced with seeded filler of
the same word count. Member data and scripts are removed.

Run locally:  python scripts/make_fixtures.py [--src data/inspect] [--out tests/fixtures]
The input folder is git-ignored and never committed.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup, Comment, NavigableString

from newsletter_archive.extract import BOILERPLATE_PREFIXES

BASE = "https://www.example.com"
FILLER_WORDS = [
    "team",
    "plan",
    "work",
    "goal",
    "idea",
    "review",
    "signal",
    "manager",
    "project",
    "trust",
    "growth",
    "feedback",
    "leader",
    "meeting",
    "decision",
    "result",
    "effort",
    "clarity",
    "owner",
    "customer",
    "metric",
    "habit",
    "scope",
    "context",
    "career",
    "promotion",
    "problem",
    "process",
    "skill",
    "change",
    "focus",
    "outcome",
    "question",
    "answer",
    "example",
    "story",
    "mentor",
    "practice",
    "writing",
    "direction",
    "priority",
]
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
NGRAM = 8


class Filler:
    """Deterministic filler text that keeps word counts and sentence shape."""

    def __init__(self, seed: str):
        self.rng = random.Random(seed)

    def words(self, n: int) -> str:
        out = []
        for i in range(n):
            w = self.rng.choice(FILLER_WORDS)
            if i == 0 or out[-1].endswith("."):
                w = w.capitalize()
            if (i + 1) % 11 == 0 or i == n - 1:
                w += "."
            out.append(w)
        return " ".join(out)


def _replace_text(node: NavigableString, filler: Filler, keep_prefix: str | None = None) -> None:
    text = str(node)
    n = len(text.split())
    if n == 0:
        return
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    if keep_prefix:
        kept = len(keep_prefix.split())
        new = keep_prefix + (" " + filler.words(n - kept) if n > kept else "")
    else:
        new = filler.words(n)
    node.replace_with(lead + new + trail)


def _strip_scripts_and_members(soup: BeautifulSoup) -> None:
    for s in soup.find_all("script"):
        if s.get("type") != "application/ld+json":
            s.decompose()
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()
    for el in soup.find_all(True):
        for attr in list(el.attrs):
            if attr in {"data-key", "data-member-uuid", "data-uuid", "data-post-id"} or (
                isinstance(el.attrs.get(attr), str) and UUID_RE.search(el.attrs[attr])
            ):
                del el.attrs[attr]
    # drop every <style> except the members block (it names .gh-post-upgrade-cta,
    # which tests use to prove the login check doesn't rely on that class)
    for st in soup.find_all("style"):
        if st.get("id") != "gh-members-styles":
            st.decompose()
    for link in soup.find_all("link", rel=lambda r: r and "stylesheet" in r):
        link.decompose()


def _fill_article_body(soup: BeautifulSoup, seed: str) -> None:
    body = soup.select_one("article.gh-article .gh-content")
    if body is None:
        raise SystemExit("no .gh-content in post page")
    filler = Filler(seed)
    first_blocks = [c for c in body.children if getattr(c, "name", None)][:3]
    for node in list(body.find_all(string=True)):
        if isinstance(node, Comment):
            continue
        parent_heading = node.find_parent(re.compile(r"^h[1-6]$"))
        if parent_heading is not None and len(parent_heading.get_text().split()) < NGRAM:
            continue  # short headings stay (they shape sections and are visible on free previews)
        block = next((b for b in first_blocks if node in b.descendants), None)
        keep = None
        if block is not None:
            block_text = " ".join(block.get_text().split())
            for prefix in BOILERPLATE_PREFIXES:
                if block_text.startswith(prefix) and str(node).strip().startswith(prefix):
                    keep = prefix
        _replace_text(node, filler, keep)


def _fill_outside_text(soup: BeautifulSoup, seed: str) -> None:
    """Related-post excerpts and the article excerpt are prose too; fill them."""
    filler = Filler(seed + "-outside")
    for el in soup.select(".gh-card-excerpt, .gh-article-excerpt"):
        for node in list(el.find_all(string=True)):
            _replace_text(node, filler)


def _finalize(html: str) -> str:
    """Replace ids (such as the public image-storage uuid) with a placeholder."""
    html = UUID_RE.sub("site-id", html)
    return EMAIL_RE.sub("noreply@example.com", html)


def _ngrams(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {tuple(words[i : i + NGRAM]) for i in range(len(words) - NGRAM + 1)}


def build_post(src: Path, out: Path, seed: str) -> None:
    html = src.read_text(encoding="utf-8")
    original_body = BeautifulSoup(html, "lxml").select_one("article.gh-article .gh-content")
    soup = BeautifulSoup(html, "lxml")
    _strip_scripts_and_members(soup)
    _fill_article_body(soup, seed)
    _fill_outside_text(soup, seed)
    result = _finalize(str(soup))
    new_body = BeautifulSoup(result, "lxml").select_one("article.gh-article .gh-content")
    overlap = _ngrams(original_body.get_text(" ")) & _ngrams(new_body.get_text(" "))
    if overlap:
        raise SystemExit(f"{out.name}: {len(overlap)} {NGRAM}-word runs copied from the source")
    if UUID_RE.search(result) or any(m != "noreply@example.com" for m in EMAIL_RE.findall(result)):
        raise SystemExit(f"{out.name}: email or uuid left after scrubbing")
    out.write_text(result, encoding="utf-8")


def build_sitemaps(src: Path, out: Path, keep_slugs: list[str]) -> None:
    (out / "sitemap-index.xml").write_text(
        _finalize((src / "sitemap.xml").read_text(encoding="utf-8")), encoding="utf-8"
    )
    posts = (src / "posts.xml").read_text(encoding="utf-8")
    urls = re.findall(r"<url>.*?</url>", posts, re.S)
    keep = [u for u in urls if any(f"/{s}/</loc>" in u for s in keep_slugs)]
    keep = [re.sub(r"<image:image>.*?</image:image>", "", u, flags=re.S) for u in keep]
    head = posts[: posts.index("<url>")]
    (out / "sitemap-posts.xml").write_text(_finalize(head + "".join(keep) + "</urlset>"), encoding="utf-8")


def build_categories(src: Path, out: Path) -> list[str]:
    soup = BeautifulSoup((src / "categories.html").read_text(encoding="utf-8"), "lxml")
    slugs = []
    for a in soup.find_all("a", href=True):
        m = re.match(rf"^(?:{re.escape(BASE)})?/t/([^/]+)/?$", a["href"])
        if m and m.group(1) not in slugs:
            slugs.append(m.group(1))
    links = "\n".join(
        f'      <li><a class="category-link" href="{BASE}/t/{s}">{s.replace("-", " ").title()}</a></li>'
        for s in slugs
    )
    page = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Categories - Example Newsletter</title></head>
<body>
  <nav class="gh-navigation">
    <a href="{BASE}/">Home</a>
    <a href="{BASE}/tag/newsletter/">Newsletter</a>
    <a href="{BASE}/about/">About</a>
    <a href="#/portal/signin" data-portal="signin">Sign in</a>
  </nav>
  <main>
    <h1>Categories</h1>
    <ul class="categories">
{links}
    </ul>
    <p>Also see <a href="/t/{slugs[0]}">{slugs[0]}</a> (relative link, same category).</p>
  </main>
</body>
</html>
"""
    (out / "categories.html").write_text(page, encoding="utf-8")
    return slugs


def build_tag_pages(src: Path, out: Path, page2_slugs: list[str]) -> list[str]:
    soup = BeautifulSoup((src / "t-career-growth.html").read_text(encoding="utf-8"), "lxml")
    _strip_scripts_and_members(soup)
    _fill_outside_text(soup, "tag")
    p1_slugs = [a["href"].strip("/") for a in soup.select("article.gh-card a.gh-card-link[href]")]
    (out / "tag-career-growth-p1.html").write_text(_finalize(str(soup)), encoding="utf-8")

    # page 2: same markup, different posts, no rel=next
    for link in soup.find_all("link", rel="next"):
        link.decompose()
    cards = soup.select("article.gh-card")
    for card in cards[len(page2_slugs) :]:
        card.decompose()
    for card, slug in zip(soup.select("article.gh-card"), page2_slugs, strict=False):
        a = card.select_one("a.gh-card-link")
        a["href"] = f"/{slug}/"
        title = card.select_one(".gh-card-title")
        if title is not None:
            title.string = slug.replace("-", " ").capitalize()
    (out / "tag-career-growth-p2.html").write_text(_finalize(str(soup)), encoding="utf-8")
    return p1_slugs


# -- Substack ----------------------------------------------------------------

SUBSTACK_BASE = "https://newsletter.example.org"
SUBSTACK_BODY = "div.available-content div.body.markup"
SUBSTACK_BLOCKS = 60  # top-level body blocks kept: enough markup kinds, a smaller file
URL_ATTRS = ("href", "src", "srcset", "data-attrs", "data-component-name", "poster")


def _fill_all_text(el, seed: str) -> None:
    filler = Filler(seed)
    for node in list(el.find_all(string=True)):
        if not isinstance(node, Comment):
            _replace_text(node, filler)


def _neutral_attrs(el) -> None:
    """Links point at example.org, media and widget data are dropped; classes stay."""
    for i, tag in enumerate([el, *el.find_all(True)]):
        for name in list(tag.attrs):
            if name == "href":
                tag["href"] = f"{SUBSTACK_BASE}/link-{i}"
            elif name in URL_ATTRS or name.startswith("data-") or name in {"id", "style", "alt", "title"}:
                del tag.attrs[name]


def _substack_page(post_html: str, seed: str, signed_in: bool) -> tuple[str, str]:
    """A structure-only Substack post page and the original body text (for the copy check).

    Kept from the real page: the body markup (tags and classes, first SUBSTACK_BLOCKS blocks)
    and the paywall block. Everything identifying is rebuilt: page data with a fake reader,
    meta tags, JSON-LD, title and subtitle."""
    src = BeautifulSoup(post_html, "lxml")
    body = src.select_one(SUBSTACK_BODY)
    if body is None:
        raise SystemExit("no Substack body in post page")
    original = body.get_text(" ")
    for extra in [c for c in body.find_all(True, recursive=False)][SUBSTACK_BLOCKS:]:
        extra.decompose()
    _fill_all_text(body, seed)
    _neutral_attrs(body)
    paywall = src.select_one("div.paywall")
    paywall_html = ""
    if paywall is not None:
        _fill_all_text(paywall, seed + "-paywall")
        _neutral_attrs(paywall)
        paywall_html = str(paywall)
    data = {
        "user": {"id": 1001, "name": "Test Reader", "handle": "test-reader"} if signed_in else None,
        "post": {
            "id": 2002,
            "slug": "example-post",
            "title": "Example Post",
            "audience": "only_paid",
            "type": "newsletter",
            "section_id": 30,
            "post_date": "2025-03-04T09:00:00.000Z",
        },
        "pub": {
            "name": "Example Engineering Letter",
            "sections": [
                {"id": 10, "name": "The Weekly", "slug": "the-weekly"},
                {"id": 30, "name": "Deep Dives", "slug": "deep-dives"},
            ],
        },
    }
    ld = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": "Example Post",
        "datePublished": "2025-03-04T09:00:00+00:00",
        "author": [{"@type": "Person", "name": "Example Author"}],
    }
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Example Post - by Example Author</title>
<meta property="og:title" content="Example Post">
<meta name="author" content="Example Author">
<meta name="description" content="Short description of the example post.">
<script type="application/ld+json">{json.dumps(ld)}</script>
<script type="application/json" id="preloads-json">{json.dumps(data)}</script>
</head>
<body>
<div id="main">
<article class="typography newsletter-post post">
<div class="post-header">
<h1 class="post-title published">Example Post</h1>
<h3 class="subtitle">A subtitle for the example post</h3>
</div>
<div class="available-content">{body}</div>
{paywall_html}
</article>
</div>
</body>
</html>
"""
    return _finalize(page), original


def build_substack_sitemap(out: Path) -> None:
    """Every URL kind the real sitemap has, plus the ones to drop: comments and off-site."""
    paths = [
        "/archive",
        "/about",
        "/podcast",
        "/p/first-example-post",
        "/p/second-example-post?utm_source=feed",
        "/p/third-example-post/",
        "/p/first-example-post/comment/12345",
    ]
    urls = [f"{SUBSTACK_BASE}{p}" for p in paths] + ["https://other.example.com/p/not-ours"]
    entries = "".join(f"<url><loc>{u}</loc><lastmod>2025-03-04</lastmod></url>" for u in urls)
    (out / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>\n',
        encoding="utf-8",
    )


def build_substack(src: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for name, signed_in in (("post-signed-in.html", True), ("post-signed-out.html", False)):
        source = src / ("post.html" if signed_in else "post-signed-out.html")
        page, original = _substack_page(source.read_text(encoding="utf-8"), name, signed_in)
        new_body = BeautifulSoup(page, "lxml").select_one(SUBSTACK_BODY)
        overlap = _ngrams(original) & _ngrams(new_body.get_text(" ") if new_body else "")
        if overlap:
            raise SystemExit(f"{name}: {len(overlap)} {NGRAM}-word runs copied from the source")
        if UUID_RE.search(page) or any(m != "noreply@example.com" for m in EMAIL_RE.findall(page)):
            raise SystemExit(f"{name}: email or uuid left after scrubbing")
        (out / name).write_text(page, encoding="utf-8")
    build_substack_sitemap(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--src", type=Path, default=Path("data/inspect"))
    ap.add_argument("--out", type=Path, default=Path("tests/fixtures"))
    ap.add_argument(
        "--substack",
        action="store_true",
        help="Build the Substack fixtures from <src>/substack/ into <out>/substack/.",
    )
    args = ap.parse_args()
    if args.substack:
        if not (args.src / "substack").is_dir():
            print(f"missing {args.src / 'substack'}; save the Substack pages there first", file=sys.stderr)
            return 1
        build_substack(args.src / "substack", args.out / "substack")
        print(f"Substack fixtures written to {args.out / 'substack'}")
        return 0
    if not args.src.is_dir():
        print(f"missing {args.src}; save the site pages there first", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)

    build_categories(args.src, args.out)
    p1 = build_tag_pages(
        args.src,
        args.out,
        page2_slugs=[
            "how-amazon-uses-tenets-to-make-better-decisions-at-every-level",
            "the-secret-to-sounding-like-a-senior-leader-say-less",
        ],
    )
    sitemap_slugs = [
        "stop-trying-harder-change-your-environment-instead",
        "how-amazon-uses-tenets-to-make-better-decisions-at-every-level",
        "the-ambitious-leaders-guide-to-avoiding-burnout",
        "the-secret-to-sounding-like-a-senior-leader-say-less",
        "guide-to-building-a-newsletter",
        "your-career-isnt-a-meritocracy-its-a-trustocracy",
    ]
    build_sitemaps(args.src, args.out, sitemap_slugs)
    build_post(args.src / "post1-auth.html", args.out / "post-signed-in.html", "signed-in")
    build_post(args.src / "post1-anon.html", args.out / "post-signed-out.html", "signed-out")
    build_post(
        args.src / "anon-guide-to-building-a-newsletter.html",
        args.out / "post-italic-opener.html",
        "italic-opener",
    )
    print(f"fixtures written to {args.out} (tag page 1 lists {len(p1)} posts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

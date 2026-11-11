"""The Substack platform against structure-only fixtures built from a real Substack page."""

from __future__ import annotations

import json
import re

import pytest
from bs4 import BeautifulSoup
from hypothesis import given
from hypothesis import strategies as st

from newsletter_archive.errors import ExtractError
from newsletter_archive.platforms import get_platform
from newsletter_archive.platforms.substack import (
    REMOVE_SELECTORS,
    SubstackPlatform,
    decode_legacy_preloads,
    preloads,
    section_categories,
)

from .conftest import fixture_text

SB = "https://newsletter.example.org"
URL = f"{SB}/p/example-post"
SUBSTACK = SubstackPlatform()


def page(name: str) -> str:
    return fixture_text(f"substack/{name}")


def with_data(html: str, **changes) -> str:
    """The page with its page data changed (top-level keys, or post fields via post={...})."""
    soup = BeautifulSoup(html, "lxml")
    script = soup.find("script", id="preloads-json")
    data = json.loads(script.string)
    post = changes.pop("post", None)
    data.update(changes)
    if post is not None:
        data["post"].update(post)
    script.string = json.dumps(data)
    return str(soup)


@pytest.mark.parametrize(
    "variant",
    [
        "/p/first-example-post?r=abc12",
        "/p/first-example-post?triedRedirect=true",
        "/p/First-Example-Post",
        "/p/first-example-post/",
        ":443/p/first-example-post#comments",
    ],
)
def test_share_and_redirect_variants_are_one_post(variant):
    urls = [f"{SB}/p/first-example-post", f"{SB}{variant}", f"https://www.newsletter.example.org{variant}"]
    assert SUBSTACK.post_urls(urls, SB) == [f"{SB}/p/first-example-post"]


def test_classes_that_only_contain_share_or_subscribe_are_kept():
    soup = BeautifulSoup(page("post-signed-in.html"), "lxml")
    body = soup.select_one("div.body.markup")
    body.insert(1, BeautifulSoup('<p class="shared-context">zebrasharedtext stays</p>', "html.parser").p)
    body.insert(
        2, BeautifulSoup('<div class="subscribed-note"><p>zebranotetext</p></div>', "html.parser").div
    )
    text = SUBSTACK.extract(str(soup), URL, (), 300).text
    assert "zebrasharedtext" in text and "zebranotetext" in text


@pytest.mark.parametrize("tag", ["img", "source", "input", "embed"])
def test_void_media_tags_are_removed(tag):
    from newsletter_archive.extract import clean_body

    body = BeautifulSoup(f"<div><p>kept <{tag} src='x'></p></div>", "html.parser").div
    assert clean_body(body, (), REMOVE_SELECTORS).find(tag) is None


def test_broken_new_page_data_falls_back_to_the_older_form():
    soup = BeautifulSoup(page("post-signed-in.html"), "lxml")
    script = soup.find("script", id="preloads-json")
    data = script.string
    script.string = "{ broken"
    legacy = soup.new_tag("script")
    legacy.string = f"window._preloads = JSON.parse({json.dumps(data)})"
    soup.head.append(legacy)
    assert SUBSTACK.signed_in(str(soup))


def test_registered_as_a_platform():
    assert isinstance(get_platform("substack"), SubstackPlatform)
    assert get_platform("substack") is get_platform("substack")


# spec: post-discovery / Read post URLs from the sitemap (Substack)
def test_post_urls_are_the_p_paths_on_this_site_only():
    urls = re.findall(r"<loc>([^<]+)</loc>", page("sitemap.xml"))
    assert SUBSTACK.post_urls(urls, SB) == [
        f"{SB}/p/first-example-post",
        f"{SB}/p/second-example-post",
        f"{SB}/p/third-example-post",
    ]  # archive, about, podcast, comment pages, off-site and duplicates are dropped


def test_no_category_crawl_and_posts_requested_on_the_site():
    assert SUBSTACK.discover_categories(client=None, base_url=SB) is None  # type: ignore[arg-type]
    assert SUBSTACK.request_url("http://NEWSLETTER.example.org/p/x", SB) == f"{SB}/p/x"


# spec: post-fetching / Signed-in check (Substack)
def test_signed_in_only_with_a_reader_in_the_page_data():
    assert SUBSTACK.signed_in(page("post-signed-in.html"))
    assert not SUBSTACK.signed_in(page("post-signed-out.html"))
    assert not SUBSTACK.signed_in(with_data(page("post-signed-in.html"), user={"name": "no id"}))
    assert not SUBSTACK.signed_in("<html><body>no page data</body></html>")


# spec: content-extraction / Article body only; Full post
def test_signed_in_post_extracts_in_full():
    ex = SUBSTACK.extract(page("post-signed-in.html"), URL, (), 300)
    assert (ex.title, ex.subtitle, ex.author, ex.published_date) == (
        "Example Post",
        "A subtitle for the example post",
        "Example Author",
        "2025-03-04",
    )
    assert ex.summary == "Short description of the example post."
    assert ex.categories == ["Deep Dives"]  # the post's section
    assert not ex.paywalled and ex.word_count > 2000
    assert [s.heading for s in ex.sections if s.heading] and all(s.text for s in ex.sections)
    assert ex.links and all(link["url"].startswith(SB) for link in ex.links)


# spec: content-extraction / Paywall detection (Truncated paid post)
def test_signed_out_paid_post_is_paywalled():
    ex = SUBSTACK.extract(page("post-signed-out.html"), URL, (), 300)
    assert ex.paywalled
    soup = BeautifulSoup(page("post-signed-out.html"), "lxml")
    assert soup.select_one("div.paywall") is not None  # the real block, kept in the fixture


def test_short_paid_post_without_the_block_is_paywalled_too():
    soup = BeautifulSoup(page("post-signed-in.html"), "lxml")
    body = soup.select_one("div.body.markup")
    for el in body.find_all(True, recursive=False)[3:]:
        el.decompose()
    short = SUBSTACK.extract(str(soup), URL, (), 300)
    assert short.word_count < 300 and short.paywalled
    free = SUBSTACK.extract(with_data(str(soup), post={"audience": "everyone"}), URL, (), 300)
    assert not free.paywalled


@pytest.mark.parametrize("selector", [s.strip() for s in REMOVE_SELECTORS.split(",")])
def test_each_kind_of_clutter_is_removed(selector):
    soup = BeautifulSoup(page("post-signed-in.html"), "lxml")
    body = soup.select_one("div.body.markup")
    marker = "zebracluttermarker"
    if selector.startswith("."):
        html = f'<div class="{selector[1:]}"><p>{marker}</p></div>'
    elif selector.startswith("[class*="):
        html = f'<div class="x-{selector.split(chr(34))[1]}-y"><p>{marker}</p></div>'
    elif selector in {"img", "source", "input", "embed"}:
        html = f'<p>kept <{selector} alt="{marker}" title="{marker}"></p>'
    else:
        html = f"<{selector}>{marker}</{selector}>"
    # Wrapped in a div so the parser keeps tags such as <script> where they were written.
    fragment = BeautifulSoup(f"<div>{html}</div>", "html.parser").div
    body.insert(1, fragment)
    ex = SUBSTACK.extract(str(soup), URL, (), 300)
    assert marker not in ex.text and all(marker not in link["text"] for link in ex.links)


def test_configured_boilerplate_opening_is_stripped():
    soup = BeautifulSoup(page("post-signed-in.html"), "lxml")
    body = soup.select_one("div.body.markup")
    body.insert(0, BeautifulSoup("<p>Hello, this is the weekly intro line.</p>", "lxml").p)
    assert "weekly intro" in SUBSTACK.extract(str(soup), URL, (), 300).text
    assert "weekly intro" not in SUBSTACK.extract(str(soup), URL, ("Hello, this is",), 300).text


def test_metadata_fallbacks():
    soup = BeautifulSoup(page("post-signed-in.html"), "lxml")
    for el in soup.select(
        'meta[property="og:title"], meta[name="author"], script[type="application/ld+json"]'
    ):
        el.decompose()
    ex = SUBSTACK.extract(str(soup), URL, (), 300)
    assert ex.title == "Example Post"  # h1.post-title
    assert ex.author == "Unknown"
    assert ex.published_date == "2025-03-04"  # post.post_date
    soup.select_one("h1.post-title").decompose()
    assert SUBSTACK.extract(str(soup), URL, (), 300).title == "Example Post"  # post.title


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda s: s.select_one("div.available-content").decompose(), "no article body"),
        (lambda s: s.find("script", id="preloads-json").decompose(), "no Substack page data"),
        (
            lambda s: setattr(s.find("script", id="preloads-json"), "string", "{ broken"),
            "no Substack page data",
        ),
        (lambda s: setattr(s.find("script", id="preloads-json"), "string", "[1]"), "no Substack page data"),
    ],
)
def test_broken_pages_fail_that_post(change, message):
    soup = BeautifulSoup(page("post-signed-in.html"), "lxml")
    change(soup)
    with pytest.raises(ExtractError, match=message):
        SUBSTACK.extract(str(soup), URL, (), 300)


def test_missing_title_or_date_fails():
    soup = BeautifulSoup(page("post-signed-in.html"), "lxml")
    for el in soup.select('meta[property="og:title"], script[type="application/ld+json"], h1.post-title'):
        el.decompose()
    no_title = with_data(str(soup), post={"title": None})
    with pytest.raises(ExtractError, match="no title"):
        SUBSTACK.extract(no_title, URL, (), 300)
    no_date = with_data(str(soup), post={"post_date": None})
    with pytest.raises(ExtractError, match="no published date"):
        SUBSTACK.extract(no_date, URL, (), 300)


@pytest.mark.parametrize(
    "data, categories",
    [
        (
            {"post": {"section_id": 30}, "pub": {"sections": [{"id": 30, "name": " Deep Dives "}]}},
            ["Deep Dives"],
        ),
        ({"post": {"section_id": None}, "pub": {"sections": [{"id": 30, "name": "X"}]}}, []),  # no section
        # the page doesn't say: None keeps the categories already stored
        ({"post": {"section_id": 99}, "pub": {"sections": [{"id": 30, "name": "X"}]}}, None),
        ({"post": {"section_id": 30}, "pub": {"sections": [{"id": 30, "name": " "}]}}, None),
        ({"post": {"section_id": 30}, "pub": {}}, None),
        ({"post": "odd", "pub": "odd"}, None),
    ],
)
def test_section_categories(data, categories):
    assert section_categories(data) == categories


# -- the older `window._preloads = JSON.parse("...")` form -------------------------------------

json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(), children, max_size=4),
    max_leaves=12,
)


@given(st.dictionaries(st.text(), json_values, max_size=6))
def test_legacy_string_literal_round_trips(data):
    literal = json.dumps(json.dumps(data))
    assert decode_legacy_preloads(literal) == data
    html = f"<html><head><script>window._preloads = JSON.parse({literal})</script></head></html>"
    assert preloads(BeautifulSoup(html, "lxml")) == data


@pytest.mark.parametrize(
    "literal", ['"not json"', '"[1, 2]"', "not a literal", json.dumps(json.dumps("text"))]
)
def test_legacy_broken_data_is_an_extract_error(literal):
    with pytest.raises(ExtractError, match="no Substack page data"):
        decode_legacy_preloads(literal)


def test_legacy_page_is_read_like_the_new_one():
    soup = BeautifulSoup(page("post-signed-in.html"), "lxml")
    script = soup.find("script", id="preloads-json")
    data = script.string
    script.decompose()
    legacy = soup.new_tag("script")
    legacy.string = f"window._preloads = JSON.parse({json.dumps(data)})"
    soup.head.append(legacy)
    assert SUBSTACK.signed_in(str(soup))
    assert SUBSTACK.extract(str(soup), URL, (), 300).categories == ["Deep Dives"]

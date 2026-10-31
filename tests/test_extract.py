import json
import logging

import pytest
from bs4 import BeautifulSoup

import newsletter_archive.extract as ex
from newsletter_archive.errors import ExtractError
from newsletter_archive.extract import (
    clean_body,
    extract_post,
    first_sentences,
    sections_from_soup,
    strip_boilerplate,
    to_iso_date,
)

from .conftest import FIXTURES, fixture_text

BASE = "https://www.example.com"
POSTS = sorted(p.name for p in FIXTURES.glob("post-*.html"))


def extract(name, slug="test-post"):
    return extract_post(fixture_text(name), f"{BASE}/{slug}")


@pytest.fixture(scope="module")
def clutter():
    return extract_post(fixture_text("post-clutter.html"), f"{BASE}/why-promotions-stall")


# spec: content-extraction / Post page with clutter
@pytest.mark.parametrize(
    "junk",
    ["NAVTEXT", "SHARETEXT", "SUBSCRIBETEXT", "RELATEDTEXT", "RELATEDCARD", "COMMENTTEXT",
     "COMMENTROOT", "CAPTIONTEXT", "HEROCAPTION", "ALTTEXT", "HEROALT", "TITLETEXT", "FOOTERLINK",
     "Subscribe", "Byline Author"],
)
def test_clutter_removed(clutter, junk):
    blob = clutter.text + json.dumps([s.__dict__ for s in clutter.sections]) + json.dumps(clutter.links)
    assert junk not in blob


# spec: content-extraction / Strip newsletter boilerplate — Standard intro
def test_boilerplate_intro_removed(clutter):
    assert not clutter.text.startswith("Welcome to my newsletter!")
    assert "Free members can read" not in clutter.text and "BOILERPLATE" not in clutter.text
    assert not any("linkedin.com/in/example" in link["url"] for link in clutter.links)
    # a quoted "Welcome to my newsletter!" further down stays
    assert 'Someone said "Welcome to my newsletter!" here' in clutter.text


def test_boilerplate_removed_from_real_structure():
    e = extract("post-signed-in.html")
    assert not e.text.startswith("Welcome to my newsletter!")
    assert "Free members can read" not in e.text


# spec: content-extraction / Strip newsletter boilerplate — Post-specific italic opener
def test_post_specific_italic_opener_kept():
    soup = BeautifulSoup(fixture_text("post-italic-opener.html"), "lxml")
    first_p = soup.select_one("article.gh-article .gh-content > p")
    assert first_p.find("em") is not None
    opener = " ".join(first_p.get_text().split())
    assert extract("post-italic-opener.html").text.startswith(opener[:40])


def test_strip_boilerplate_stops_at_first_real_paragraph():
    body = BeautifulSoup(
        "<section><p>Real start.</p><p>Welcome to my newsletter! later</p></section>", "lxml"
    ).section
    assert strip_boilerplate(body) == 0
    assert "Welcome" in body.get_text()


def test_clean_body_does_not_modify_input():
    soup = BeautifulSoup(fixture_text("post-clutter.html"), "lxml")
    body = soup.select_one(".gh-content")
    before = str(body)
    clean_body(body)
    assert str(body) == before


# spec: content-extraction / Intro and two headings
def test_sections_from_headings(clutter):
    heads = [(s.heading, s.level) for s in clutter.sections]
    assert heads == [(None, 0), ("Why promotions stall", 2), ("Example", 3)]
    intro = clutter.sections[0].text
    assert intro.startswith("Intro paragraph about stalled promotions.")
    assert "Second intro paragraph" in intro


def test_lists_quotes_tables_rendered(clutter):
    assert "- First point\n- Second point" in clutter.sections[1].text
    assert "Level | Scope\nL5 | Team" in clutter.sections[2].text


def test_empty_sections_dropped(clutter):
    assert all(s.text.strip() for s in clutter.sections)
    assert "Empty heading section" not in [s.heading for s in clutter.sections]


# spec: content-extraction / Invariant holds
@pytest.mark.parametrize("name", POSTS)
def test_text_equals_joined_sections(name):
    e = extract(name)
    assert "\n\n".join(s.text for s in e.sections) == e.text
    assert e.word_count == len(e.text.split())


def test_real_structure_sections():
    e = extract("post-signed-in.html")
    assert [s.level for s in e.sections] == [0, 2, 2, 2, 2, 2, 2]
    assert e.sections[1].heading == "Good intentions."
    assert e.word_count > 2000


# spec: content-extraction / Meta tags present
def test_metadata_from_meta_and_json_ld(clutter):
    assert clutter.title == "Why Promotions Stall"  # og:title beats JSON-LD headline
    assert clutter.author == "Test Author"  # JSON-LD beats the visible byline
    assert clutter.published_date == "2025-03-05"  # 2025-03-04T23:30-08:00 in UTC
    assert clutter.subtitle == "A subtitle for the clutter post."
    assert clutter.summary == "A synthetic post used to test that clutter is removed."


def test_metadata_from_real_structure():
    e = extract("post-signed-in.html")
    assert e.title == "Stop Trying Harder. Change Your Environment Instead."
    assert e.author == "David Anderson"
    assert e.published_date == "2026-10-26"
    assert e.summary.startswith("I cover why willpower")


# spec: content-extraction / Missing og:title
def test_metadata_fallbacks():
    e = extract("post-meta-fallback.html")
    assert e.title == "Title From H1"
    assert e.author == "Byline Author"
    assert e.published_date == "2024-12-31"
    assert e.subtitle is None
    assert e.summary == "First sentence of the post. Second sentence follows here!"


def test_title_falls_back_to_title_tag_and_author_to_unknown(caplog):
    html = (
        "<html><head><title>Only Title - Example Newsletter</title>"
        '<meta property="article:published_time" content="2024-01-02T00:00:00Z"></head>'
        '<body><article class="gh-article"><section class="gh-content"><p>Hi there.</p></section></article></body></html>'
    )
    e = extract_post(html, f"{BASE}/x")
    assert e.title == "Only Title" and e.author == "Unknown"


def test_missing_date_raises():
    html = '<html><body><article class="gh-article"><h1>T</h1><section class="gh-content"><p>x</p></section></article></body></html>'
    with pytest.raises(ExtractError, match="published date"):
        extract_post(html, f"{BASE}/x")


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-10-26T14:27:00.000Z", "2026-10-26"),
        ("2025-03-04T23:30:00-08:00", "2025-03-05"),
        ("2024-12-31", "2024-12-31"),
        ("2024-12-31T10:00:00", "2024-12-31"),
        ("garbage", None),
        (None, None),
    ],
)
def test_to_iso_date(value, expected):
    assert to_iso_date(value) == expected


def test_first_sentences_caps_length():
    assert first_sentences("One. Two! Three?") == "One. Two!"
    long = ("word " * 200).strip() + "."
    assert len(first_sentences(long)) <= 401


# spec: content-extraction / Links in body and footer
def test_links_body_only_absolute_deduped(clutter):
    assert clutter.links == [
        {"text": "another post", "url": f"{BASE}/another-post/"},
        {"text": "reference", "url": "https://example.org/ref"},
    ]


# spec: content-extraction / Truncated paid post; Full post; Paywall box inside the body
def test_paywall_box_in_body_flags_post():
    assert extract("post-signed-out.html").paywalled is True
    assert extract("post-signed-in.html").paywalled is False  # class only in CSS


def test_paywall_cta_text_not_in_output():
    e = extract("post-signed-out.html")
    assert "paying subscribers only" not in e.text
    assert e.word_count < extract("post-signed-in.html").word_count


# fallback path
def test_soup_walker_used_when_trafilatura_drops_headings(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="newsletter_archive")
    monkeypatch.setattr(ex, "_trafilatura_xml", lambda html: "<doc><main><p>only one paragraph</p></main></doc>")
    e = extract_post(fixture_text("post-clutter.html"), f"{BASE}/why-promotions-stall")
    assert [s.heading for s in e.sections] == [None, "Why promotions stall", "Example"]
    assert "plain HTML walker" in caplog.text


def test_soup_walker_matches_trafilatura_shape():
    soup = BeautifulSoup(fixture_text("post-clutter.html"), "lxml")
    sections = sections_from_soup(clean_body(soup.select_one(".gh-content")))
    assert [(s.heading, s.level) for s in sections] == [(None, 0), ("Why promotions stall", 2), ("Example", 3)]
    assert "- First point\n- Second point" in sections[1].text


def test_whole_page_fallback_without_gh_content():
    html = (
        '<html><head><meta property="og:title" content="T">'
        '<meta property="article:published_time" content="2024-01-02T00:00:00Z"></head><body><main>'
        + "<p>" + "Sentence with enough words to count as content. " * 20 + "</p></main></body></html>"
    )
    e = extract_post(html, f"{BASE}/x")
    assert e.word_count > 50 and e.links == []


def test_no_body_at_all_raises():
    with pytest.raises(ExtractError):
        extract_post("<html><head><title>x</title></head><body></body></html>", f"{BASE}/x")

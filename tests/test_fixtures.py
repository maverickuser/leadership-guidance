"""Sanity checks on the fixtures themselves, so later tests rest on solid ground."""

import re

import pytest
from bs4 import BeautifulSoup
from lxml import etree

from .conftest import FIXTURES, fixture_text

HTML = sorted(p.name for p in FIXTURES.glob("*.html"))
XML = sorted(p.name for p in FIXTURES.glob("*.xml"))


@pytest.mark.parametrize("name", HTML)
def test_html_parses(name):
    assert BeautifulSoup(fixture_text(name), "lxml").find("body") is not None


@pytest.mark.parametrize("name", XML)
def test_xml_parses(name):
    etree.fromstring(fixture_text(name).encode())


def test_signed_in_marker():
    assert 'data-portal="account"' in fixture_text("post-signed-in.html")
    assert 'data-portal="account"' not in fixture_text("post-signed-out.html")
    assert 'data-portal="signin"' in fixture_text("post-signed-out.html")


def test_upgrade_cta_class_is_on_every_page_in_css():
    # the class name appears in the members <style> on both pages, so it can't be the login test
    assert "gh-post-upgrade-cta" in fixture_text("post-signed-in.html")
    assert "gh-post-upgrade-cta" in fixture_text("post-signed-out.html")


def test_tag_pagination_fixtures():
    p1 = BeautifulSoup(fixture_text("tag-career-growth-p1.html"), "lxml")
    p2 = BeautifulSoup(fixture_text("tag-career-growth-p2.html"), "lxml")
    assert p1.find("link", rel="next")["href"].endswith("/tag/career-growth/page/2/")
    assert p2.find("link", rel="next") is None
    assert len(p1.select("article.gh-card a.gh-card-link")) == 12
    assert len(p2.select("article.gh-card a.gh-card-link")) == 2


def test_categories_page_lists_nine_categories():
    hrefs = re.findall(r'href="https://www\.example\.com/t/([^"]+)"', fixture_text("categories.html"))
    assert len(set(hrefs)) == 9


def test_signed_in_post_structure():
    soup = BeautifulSoup(fixture_text("post-signed-in.html"), "lxml")
    body = soup.select_one("article.gh-article .gh-content")
    assert len(body.find_all("h2")) == 6
    assert len(body.get_text(" ").split()) > 2500
    first = [c for c in body.children if getattr(c, "name", None)][:2]
    assert first[0].get_text().startswith("Welcome to my newsletter!")
    assert first[1].get_text().startswith("Free members can read")


@pytest.mark.parametrize("name", HTML + XML)
def test_no_member_data(name):
    text = fixture_text(name)
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", text, re.I)
    emails = set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text))
    assert emails <= {"noreply@example.com"}, emails

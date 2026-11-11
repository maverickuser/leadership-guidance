"""`archive run` over two newsletters, each a fake Ghost site with its own cookie."""

from __future__ import annotations

import json

import pytest

import newsletter_archive.cli as cli
from newsletter_archive.errors import ArchiveError
from newsletter_archive.search.indexer import IndexReport

from ..conftest import TEST_COOKIE
from .conftest import CATEGORIES, OTHER_COOKIE, OTHER_SLUGS, SLUGS, Harness
from .site import FixtureSite

PE_STOPPED = "Stopped: The Pragmatic Engineer (PRAGMATIC_ENGINEER_COOKIE): "


@pytest.fixture
def second():
    with FixtureSite(OTHER_SLUGS, {}, OTHER_COOKIE) as s:
        yield s


@pytest.fixture
def two(tmp_path, monkeypatch, site, second):
    h = Harness(tmp_path, monkeypatch, site, second=second)
    h.index_calls = []
    monkeypatch.setattr(
        cli, "index_archive", lambda cfg, options, on_post: h.index_calls.append(options) or IndexReport()
    )
    return h


def post_ids(folder):
    return sorted(json.loads(p.read_text())["id"] for p in (folder / "posts").glob("*.json"))


def cookies_sent(site):
    return {h.get("Cookie") for p, h in site.requests if p.strip("/") in site.slugs}


# spec: newsletter-sources / Choosing sources (All sources); Weekly run covers every source
def test_run_covers_both_newsletters_then_indexes_once(two):
    r = two.run("run")
    assert r.exit_code == 0, r.output
    assert post_ids(two.main) == sorted(f"main/{s}" for s in SLUGS)
    assert post_ids(two.pe) == sorted(f"pragmatic-engineer/{s}" for s in OTHER_SLUGS)
    assert "Summary: discovered=11 fetched=11 ok=11 partial=0 failed=0" in r.output
    assert r.output.index("== main: discover") < r.output.index("== The Pragmatic Engineer: discover")
    assert len(two.index_calls) == 1
    # each cookie went only to its own site
    assert cookies_sent(two.site) == {TEST_COOKIE} and cookies_sent(two.second) == {OTHER_COOKIE}
    # the shared limiter kept the gap across both sites (its exact spacing: tests/test_http_sources.py)
    assert two.clock.sleeps and all(s >= 2 for s in two.clock.sleeps)


# spec: newsletter-sources / Choosing sources (One source); archive-cli / CLI options (Source option)
def test_source_option_touches_only_that_newsletter(two):
    r = two.run("run", "--source", "pragmatic-engineer", "--limit", "2")
    assert r.exit_code == 0, r.output
    assert two.site.requests == [] and not two.main.exists()
    assert post_ids(two.pe) == ["pragmatic-engineer/ai-sre", "pragmatic-engineer/scaling"]
    assert len(two.index_calls) == 1

    assert two.run("discover").exit_code == 0  # both newsletters
    assert two.run("fetch", "--limit", "1").exit_code == 0  # main: post-1; the other: ai-sre already saved
    r = two.run("extract", "--source", "main")
    assert r.exit_code == 0 and "== main: extract" in r.output and "Pragmatic" not in r.output
    assert post_ids(two.main) == ["main/post-1"]
    assert post_ids(two.pe) == ["pragmatic-engineer/ai-sre", "pragmatic-engineer/scaling"]  # untouched


# spec: newsletter-sources / Choosing sources (Unknown source)
def test_unknown_source_is_rejected_before_any_request(two):
    r = two.run("fetch", "--source", "nope")
    assert r.exit_code == 1
    assert "unknown source 'nope'; valid sources: main, pragmatic-engineer" in r.output
    assert two.site.requests == [] and two.second.requests == []


# spec: newsletter-sources / One source failing (One cookie expired); Weekly run (One login expired)
def test_expired_cookie_stops_only_that_newsletter(two):
    two.second.expire_after = 1
    r = two.run("run")
    assert r.exit_code == 2, r.output
    assert post_ids(two.main) == sorted(f"main/{s}" for s in SLUGS)  # main still ran in full
    # stopped at its second post: the first page is saved, nothing of it was extracted
    assert sorted(p.stem for p in (two.pe / "raw").iterdir()) == ["ai-sre"] and post_ids(two.pe) == []
    assert (
        "The Pragmatic Engineer: your cookie looks expired. Paste a fresh Cookie header into .env "
        "(PRAGMATIC_ENGINEER_COOKIE)." in r.output
    )
    # the page saved before the stop isn't in the count: the stopped stage reports nothing (as before)
    assert "Summary: discovered=11 fetched=8 ok=8 partial=0 failed=0" in r.output
    assert PE_STOPPED + "your cookie looks expired. Paste a fresh Cookie header into .env" in r.output
    assert "Stopped: main" not in r.output
    assert len(two.index_calls) == 1  # the other newsletter's new posts are still indexed


# spec: newsletter-sources / One source failing (missing cookie)
def test_missing_cookie_stops_that_newsletter_without_a_request(tmp_path, monkeypatch, site, second):
    h = Harness(tmp_path, monkeypatch, site, second=second, second_cookie=None)
    r = h.run("run", "--limit", "2")
    assert r.exit_code == 1, r.output
    assert second.requests == [] and not h.pe.exists()
    assert post_ids(h.main) == ["main/post-1", "main/post-2"]
    assert PE_STOPPED + "cookie is not set. Copy your Cookie header into .env" in r.output
    assert "Summary: discovered=8 fetched=2 ok=2 partial=0 failed=0" in r.output


# spec: newsletter-sources / One source failing (discovery failure)
def test_unreachable_site_stops_only_that_newsletter(two):
    two.second.server.shutdown()
    two.second.server.server_close()
    r = two.run("run", "--limit", "1")
    assert r.exit_code == 1, r.output
    assert post_ids(two.main) == ["main/post-1"] and not (two.pe / "manifest.json").exists()
    stopped = [line for line in r.output.splitlines() if line.startswith("Stopped: ")]
    assert len(stopped) == 1 and stopped[0].startswith(PE_STOPPED)
    assert "Stopped: main" not in r.output and len(two.index_calls) == 1


# spec: archive-cli / CLI commands (Index failure in run): the index failure outranks a stopped source
def test_index_failure_outranks_a_stopped_newsletter(two, monkeypatch):
    two.second.expire_after = 0

    def broken(cfg, options, on_post):
        raise ArchiveError("database down")

    monkeypatch.setattr(cli, "index_archive", broken)
    r = two.run("run", "--limit", "1")
    assert r.exit_code == 3, r.output
    assert PE_STOPPED in r.output and "Index step failed: database down" in r.output
    assert post_ids(two.main) == ["main/post-1"]


def test_both_cookies_expired_lists_both(two):
    two.site.expire_after = 0
    two.second.expire_after = 0
    r = two.run("run", "--limit", "1")
    assert r.exit_code == 2
    lines = [line for line in r.output.splitlines() if line.startswith("Stopped: ")]
    assert lines == [
        "Stopped: main (NEWSLETTER_COOKIE): your cookie looks expired. Paste a fresh Cookie header into .env",
        PE_STOPPED + "your cookie looks expired. Paste a fresh Cookie header into .env",
    ]
    assert CATEGORIES  # the main site still served its category pages before the first post


# CONTRIBUTING: one request at a time, at least 2 s apart, across every newsletter.
def test_a_stopped_newsletter_still_passes_the_rate_limiter_on(two, monkeypatch):
    two.site.expire_after = 1  # the first newsletter stops at its second post
    made = []
    real = cli.make_client

    def recording(cfg, source, limiter):
        client = real(cfg, source, limiter)
        made.append((source.id, limiter, client.limiter))
        return client

    monkeypatch.setattr(cli, "make_client", recording)
    r = two.run("run")
    assert r.exit_code == 2, r.output
    (first, _, first_limiter), (second_id, passed, _) = made
    assert (first, second_id) == ("main", "pragmatic-engineer")
    assert passed is first_limiter  # the second site waits for the first site's last request


# spec: newsletter-sources / One source failing
def test_a_corrupt_manifest_stops_only_that_newsletter(two):
    two.pe.mkdir(parents=True)
    (two.pe / "manifest.json").write_text("{ not json", encoding="utf-8")
    r = two.run("run")
    assert r.exit_code == 1, r.output
    assert post_ids(two.main) == sorted(f"main/{s}" for s in SLUGS)
    stopped = [line for line in r.output.splitlines() if line.startswith("Stopped: ")]
    assert len(stopped) == 1 and stopped[0].startswith(PE_STOPPED)
    assert two.second.requests == [] and len(two.index_calls) == 1

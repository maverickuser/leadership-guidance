"""End-to-end runs of the real CLI and real HTTP stack against a local fake Ghost site."""

from __future__ import annotations

import itertools
import json
import time

import pytest

from scarlet_archive.errors import NOT_SIGNED_IN_MESSAGE
from scarlet_archive.schema import validate_post

from ..conftest import SECRET_PARTS, TEST_COOKIE
from .conftest import SLUGS, Harness
from .site import FixtureSite


def posts(h):
    return {p.stem: json.loads(p.read_text()) for p in (h.data / "posts").glob("*.json")}


# I1
def test_first_run_limit_5(harness):
    r = harness.run("run", "--limit", "5")
    assert r.exit_code == 0, r.output
    manifest = json.loads((harness.data / "manifest.json").read_text())
    assert [p["slug"] for p in manifest["posts"]] == SLUGS
    assert len(list((harness.data / "raw").iterdir())) == 5
    assert len(list((harness.data / "logs").iterdir())) == 1
    out = posts(harness)
    assert sorted(out) == SLUGS[:5]
    for rec in out.values():
        validate_post(rec)
        assert rec["status"] == "ok"
        assert not rec["text"].startswith("Welcome to my newsletter!")
    assert out["post-1"]["categories"] == ["Career Growth", "Managing Up"]  # page 2 of Managing Up
    assert out["post-4"]["categories"] == ["Managing Up"]
    assert out["post-5"]["categories"] == []
    assert "Summary: discovered=8 fetched=5 ok=5 partial=0 failed=0" in r.output


# I2
def test_second_run_is_idempotent(harness):
    harness.run("run", "--limit", "5")
    first_posts = list(harness.site.post_requests())
    mtimes = {p.name: p.stat().st_mtime_ns for p in (harness.data / "posts").iterdir()}
    r = harness.run("run", "--limit", "5")
    assert r.exit_code == 0
    assert harness.site.post_requests() == first_posts  # no new post requests
    assert {p.name: p.stat().st_mtime_ns for p in (harness.data / "posts").iterdir()} == mtimes


# I3
def test_cookie_expires_mid_run(harness):
    harness.site.expire_after = 2
    r = harness.run("run")
    assert r.exit_code == 2 and NOT_SIGNED_IN_MESSAGE in r.output
    assert sorted(p.stem for p in (harness.data / "raw").iterdir()) == ["post-1", "post-2"]
    manifest = {p["slug"]: p for p in json.loads((harness.data / "manifest.json").read_text())["posts"]}
    assert manifest["post-3"]["status"] == "discovered" and manifest["post-3"]["last_attempt"] is None


# I4
def test_flaky_post_recovers_with_backoff(harness):
    harness.site.flaky["post-1"] = [503, 503]
    r = harness.run("run", "--limit", "1")
    assert r.exit_code == 0 and "fetched=1" in r.output
    assert 2 in harness.clock.sleeps and 4 in harness.clock.sleeps


# I5
def test_404_post_fails_others_succeed(harness):
    harness.site.missing.add("post-2")
    r = harness.run("run", "--limit", "3")
    assert r.exit_code == 0 and "failed=1" in r.output and "ok=2" in r.output
    manifest = {p["slug"]: p for p in json.loads((harness.data / "manifest.json").read_text())["posts"]}
    assert manifest["post-2"]["status"] == "failed" and manifest["post-2"]["error"] == "HTTP 404"


# I6
def test_offsite_redirect_never_sends_cookie(harness):
    with FixtureSite(["elsewhere"], {}, TEST_COOKIE) as other:
        harness.site.offsite["post-1"] = f"{other.base}/elsewhere/"
        r = harness.run("run", "--limit", "1")
        assert r.exit_code == 0
        assert other.requests == []
    manifest = json.loads((harness.data / "manifest.json").read_text())["posts"]
    assert manifest[0]["status"] == "failed" and "another site" in manifest[0]["error"]


def test_server_set_cookie_is_never_resent(harness):
    harness.run("run", "--limit", "3")
    sent = [h.get("Cookie") for p, h in harness.site.requests if p.strip("/") in SLUGS]
    assert sent and all(c == TEST_COOKIE for c in sent)
    # the server's Set-Cookie (ghost-members-ssr=SIGSECRET456) is never stored and merged in
    assert not any(
        "ghost-members-ssr=SIGSECRET456" in (h.get("Cookie") or "") for _, h in harness.site.requests
    )


# I7
def test_extract_works_after_server_is_gone(tmp_path, monkeypatch):
    with FixtureSite(SLUGS, {}, TEST_COOKIE) as site:
        h = Harness(tmp_path, monkeypatch, site)
        assert h.run("discover").exit_code == 0
        assert h.run("fetch", "--limit", "2").exit_code == 0
    r = h.run("extract")  # server stopped; extract reads data/raw only
    assert r.exit_code == 0 and "ok=2" in r.output
    assert sorted(posts(h)) == ["post-1", "post-2"]


# I8
def test_verbose_run_leaks_no_cookie(harness):
    r = harness.run("run", "--limit", "5", "--verbose")
    assert r.exit_code == 0
    for part in SECRET_PARTS:
        assert part not in r.output
        for path in harness.data.rglob("*"):
            if path.is_file():
                assert part not in path.read_text(errors="replace"), path


# I9
@pytest.mark.slow
def test_real_clock_spacing(tmp_path, monkeypatch):
    with FixtureSite(["post-1", "post-2"], {}, TEST_COOKIE) as site:
        h = Harness(tmp_path, monkeypatch, site, real_clock=True)
        stamps = []
        import scarlet_archive.http as http_mod

        real_follow = http_mod.Client._follow

        def timed(self, url):
            stamps.append(time.monotonic())
            return real_follow(self, url)

        monkeypatch.setattr(http_mod.Client, "_follow", timed)
        assert h.run("discover").exit_code == 0  # sitemap index, posts sitemap, categories
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    assert len(gaps) == 2 and all(g >= 1.99 for g in gaps)


# I10
def test_fetch_dry_run_makes_no_post_requests(harness):
    harness.run("discover")
    r = harness.run("fetch", "--dry-run")
    assert r.exit_code == 0 and "post-1  fetch  would-fetch" in r.output
    assert harness.site.post_requests() == []
    assert not (harness.data / "raw").exists()

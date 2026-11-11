"""`archive run` over a fake Ghost site and a fake Substack site, side by side."""

from __future__ import annotations

import json

from .cli_helpers import BASE, SINGLE_SOURCE_CONFIG, CliEnv
from .conftest import TEST_COOKIE, fixture_text
from .sitebuilder import ghost_site

SB = "https://newsletter.example.org"
SB_COOKIE = "connect" + ".sid=TESTSECRET123"
SB_SLUGS = ["first-example-post", "second-example-post", "third-example-post"]
GHOST_SLUGS = ["p1", "p2"]


def two_platforms(tmp_path, monkeypatch, *, signed_out: set[str] = frozenset()):
    e = CliEnv(tmp_path, monkeypatch)
    e.config.write_text(
        SINGLE_SOURCE_CONFIG
        + "  - id: pragmatic-engineer\n    name: The Pragmatic Engineer\n    platform: substack\n"
        f"    base_url: {SB}\n    cookie_env: PRAGMATIC_ENGINEER_COOKIE\n",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        f"NEWSLETTER_COOKIE={TEST_COOKIE}\nPRAGMATIC_ENGINEER_COOKIE={SB_COOKIE}\n", encoding="utf-8"
    )
    ghost_site(e.transport, BASE, GHOST_SLUGS)
    e.transport.add(f"{SB}/sitemap.xml", (200, fixture_text("substack/sitemap.xml")))
    for slug in SB_SLUGS:
        name = "post-signed-out.html" if slug in signed_out else "post-signed-in.html"
        e.transport.add(f"{SB}/p/{slug}", (200, fixture_text(f"substack/{name}")))
    return e


def records(folder):
    return {p.stem: json.loads(p.read_text()) for p in (folder / "posts").glob("*.json")}


# spec: newsletter-sources / Weekly run covers every source (Both sources updated)
def test_ghost_and_substack_in_one_run(tmp_path, monkeypatch):
    e = two_platforms(tmp_path, monkeypatch)
    r = e.run("run")
    assert r.exit_code == 0, r.output
    pe = records(e.data / "pragmatic-engineer")
    assert sorted(pe) == SB_SLUGS
    first = pe["first-example-post"]
    assert first["id"] == "pragmatic-engineer/first-example-post" and first["source"] == "pragmatic-engineer"
    assert first["categories"] == ["Deep Dives"] and first["status"] == "ok"
    assert sorted(records(e.data / "main")) == GHOST_SLUGS
    # no category crawl on Substack: only the sitemap and the three posts were requested there
    sb_urls = [u for u in e.transport.urls() if u.startswith(SB)]
    assert sorted(sb_urls) == sorted([f"{SB}/sitemap.xml", *(f"{SB}/p/{s}" for s in SB_SLUGS)])
    # each cookie went only to its own site
    for call in e.transport.calls:
        expected = SB_COOKIE if call.url.startswith(SB) else TEST_COOKIE
        assert call.headers.get("Cookie") == expected, call.url
    manifest = json.loads((e.data / "pragmatic-engineer" / "manifest.json").read_text())
    assert {p["slug"]: p["categories"] for p in manifest["posts"]}["first-example-post"] == ["Deep Dives"]


# spec: newsletter-sources / One source failing (One cookie expired)
def test_expired_substack_login_stops_only_that_newsletter(tmp_path, monkeypatch):
    e = two_platforms(tmp_path, monkeypatch, signed_out={"first-example-post"})
    r = e.run("run")
    assert r.exit_code == 2, r.output
    assert (
        "The Pragmatic Engineer: your cookie looks expired. Paste a fresh Cookie header into .env "
        "(PRAGMATIC_ENGINEER_COOKIE)." in r.output
    )
    assert sorted(records(e.data / "main")) == GHOST_SLUGS
    assert not (e.data / "pragmatic-engineer" / "raw" / "first-example-post.html").exists()

import json

import pytest

from scarlet_archive.config import Secret
from scarlet_archive.errors import MissingCookieError, NotSignedInError
from scarlet_archive.fetch import fetch_url, run_fetch
from scarlet_archive.http import Client, TransportConnectionError
from scarlet_archive.models import Manifest, ManifestEntry
from scarlet_archive.store import load_manifest

from .conftest import make_config
from .fakes import FakeClock, FakeTransport
from .sitebuilder import post_page

BASE = "https://www.scarletink.com"


def manifest_of(*slugs):
    m = Manifest()
    for s in slugs:
        m.entries[s] = ManifestEntry(url=f"{BASE}/{s}", slug=s)
    return m


@pytest.fixture
def clock():
    return FakeClock()


def setup(cfg, clock, slugs, signed_in=None, overrides=None):
    signed_in = signed_in or {}
    t = FakeTransport(clock=clock)
    for s in slugs:
        t.add(f"{BASE}/{s}/", (200, post_page(s, signed_in.get(s, True))))
    for url, results in (overrides or {}).items():
        t.routes[url] = list(results)
    return t, Client(cfg, transport=t, sleep=clock.sleep, clock=clock.clock)


def test_fetch_url_adds_trailing_slash():
    assert fetch_url(ManifestEntry(url=f"{BASE}/foo", slug="foo")) == f"{BASE}/foo/"
    assert fetch_url(ManifestEntry(url=f"{BASE}/foo?a=1", slug="foo")) == f"{BASE}/foo/?a=1"


# spec: post-fetching / Successful fetch; Normalized URL in manifest; Cookie present
def test_fetch_saves_raw_and_updates_manifest(cfg, clock):
    m = manifest_of("a", "b")
    t, c = setup(cfg, clock, ["a", "b"])
    calls = []
    result = run_fetch(c, cfg, m, on_post=lambda *a: calls.append(a))
    assert result.fetched == 2
    assert t.urls() == [f"{BASE}/a/", f"{BASE}/b/"]
    assert all("TESTSECRET123" in call.headers["Cookie"] for call in t.calls)
    assert (cfg.raw_dir / "a.html").read_text().count('data-portal="account"') == 1
    saved = load_manifest(cfg.manifest_path)
    assert saved.entries["a"].status == "fetched" and saved.entries["a"].last_attempt.endswith("Z")
    assert calls == [("a", "fetch", "fetched", None), ("b", "fetch", "fetched", None)]


# spec: post-fetching / Second run
def test_second_run_makes_no_requests(cfg, clock):
    m = manifest_of("a", "b")
    t, c = setup(cfg, clock, ["a", "b"])
    run_fetch(c, cfg, m)
    before = len(t.calls)
    result = run_fetch(c, cfg, m)
    assert len(t.calls) == before and result.skipped == 2 and result.fetched == 0


# spec: post-fetching / Refresh
def test_refresh_downloads_again(cfg, clock):
    m = manifest_of("a")
    t, c = setup(cfg, clock, ["a"])
    run_fetch(c, cfg, m)
    run_fetch(c, cfg, m, refresh=True)
    assert t.urls() == [f"{BASE}/a/", f"{BASE}/a/"]


# spec: archive-cli / Limit (limit is applied before skipping)
def test_limit_picks_first_n_then_skips(cfg, clock):
    slugs = [f"p{i}" for i in range(8)]
    m = manifest_of(*slugs)
    t, c = setup(cfg, clock, slugs)
    run_fetch(c, cfg, m, limit=5)
    assert len(t.calls) == 5
    run_fetch(c, cfg, m, limit=5)
    assert len(t.calls) == 5  # same five, all cached


# spec: post-fetching / Expired cookie on first post
def test_signed_out_first_post_stops_without_writing(cfg, clock):
    m = manifest_of("a", "b")
    t, c = setup(cfg, clock, ["a", "b"], signed_in={"a": False})
    with pytest.raises(NotSignedInError) as e:
        run_fetch(c, cfg, m)
    assert e.value.slug == "a" and "cookie looks expired" in str(e.value)
    assert not (cfg.raw_dir / "a.html").exists()
    assert m.entries["a"].status == "discovered" and m.entries["a"].last_attempt is None
    assert t.urls() == [f"{BASE}/a/"]


# spec: post-fetching / Cookie expires mid-run
def test_cookie_expires_mid_run(cfg, clock):
    slugs = ["p1", "p2", "p3", "p4", "p5"]
    m = manifest_of(*slugs)
    t, c = setup(cfg, clock, slugs, signed_in={"p3": False, "p4": False, "p5": False})
    with pytest.raises(NotSignedInError):
        run_fetch(c, cfg, m)
    assert sorted(p.name for p in cfg.raw_dir.iterdir()) == ["p1.html", "p2.html"]
    saved = load_manifest(cfg.manifest_path)
    assert [saved.entries[s].status for s in slugs] == ["fetched", "fetched", "discovered", "discovered", "discovered"]


def test_upgrade_cta_class_alone_does_not_mean_signed_out(cfg, clock):
    # the signed-in fixture contains "gh-post-upgrade-cta" in its CSS; it must still be accepted
    m = manifest_of("a")
    t, c = setup(cfg, clock, ["a"])
    assert run_fetch(c, cfg, m).fetched == 1


# spec: post-fetching / Cookie missing
def test_missing_cookie_makes_no_requests(tmp_path, clock):
    cfg = make_config(tmp_path, cookie=Secret(""))
    m = manifest_of("a")
    t, c = setup(cfg, clock, ["a"])
    for dry in (False, True):
        with pytest.raises(MissingCookieError, match="SCARLETINK_COOKIE"):
            run_fetch(c, cfg, m, dry_run=dry)
    assert t.calls == []


# spec: post-fetching / Client error; post-storage / Failed post
def test_404_marks_failed_and_continues(cfg, clock):
    m = manifest_of("a", "b")
    t, c = setup(cfg, clock, ["b"], overrides={f"{BASE}/a/": [404]})
    result = run_fetch(c, cfg, m)
    assert (result.failed, result.fetched) == (1, 1)
    saved = load_manifest(cfg.manifest_path).entries["a"]
    assert saved.status == "failed" and saved.error == "HTTP 404" and saved.last_attempt


# spec: post-fetching / Retries exhausted
def test_connection_errors_exhausted_marks_failed(cfg, clock):
    m = manifest_of("a", "b")
    t, c = setup(cfg, clock, ["b"], overrides={f"{BASE}/a/": [TransportConnectionError]})
    run_fetch(c, cfg, m)
    assert m.entries["a"].status == "failed" and "after 4 attempts" in m.entries["a"].error
    assert m.entries["b"].status == "fetched"


def test_failed_post_retried_next_run(cfg, clock):
    m = manifest_of("a")
    t, c = setup(cfg, clock, [], overrides={f"{BASE}/a/": [404, (200, post_page("a"))]})
    run_fetch(c, cfg, m)
    run_fetch(c, cfg, m)
    assert m.entries["a"].status == "fetched" and m.entries["a"].error is None


def test_manifest_saved_after_each_post(cfg, clock, monkeypatch):
    import scarlet_archive.fetch as fetch_mod

    m = manifest_of("a", "b")
    t, c = setup(cfg, clock, ["a", "b"])
    saves = []
    real = fetch_mod.save_manifest
    monkeypatch.setattr(fetch_mod, "save_manifest", lambda man, p: (saves.append(1), real(man, p)))
    run_fetch(c, cfg, m)
    assert len(saves) == 2


# spec: archive-cli / Dry run
def test_dry_run_makes_no_requests_and_writes_nothing(cfg, clock):
    m = manifest_of("a", "b")
    t, c = setup(cfg, clock, ["a", "b"])
    result = run_fetch(c, cfg, m, dry_run=True)
    assert result.would_fetch == ["a", "b"]
    assert t.calls == [] and not cfg.raw_dir.exists() and not cfg.manifest_path.exists()

import hashlib
import json
import os

import pytest

import scarlet_archive.store as store
from scarlet_archive.errors import ExtractError
from scarlet_archive.models import Extracted, Manifest, ManifestEntry, Section
from scarlet_archive.schema import validate_post
from scarlet_archive.store import build_record, load_manifest, run_extract, write_text_atomic

from .conftest import fixture_text
from .sitebuilder import post_page

BASE = "https://www.scarletink.com"


def seed(cfg, slugs, html=None, categories=None):
    m = Manifest()
    for s in slugs:
        m.entries[s] = ManifestEntry(url=f"{BASE}/{s}", slug=s, categories=list(categories or []), status="fetched")
        write_text_atomic(cfg.raw_dir / f"{s}.html", html or post_page(s))
    return m


def sample_extracted(words=500, paywalled=False):
    text = " ".join(["word"] * words)
    return Extracted(
        title="T", subtitle=None, author="A", published_date="2024-01-02", summary="S",
        sections=[Section(None, 0, text)], text=text, links=[{"text": "x", "url": "https://x.test/"}],
        word_count=words, paywalled=paywalled,
    )


# spec: post-storage / Valid output
def test_record_validates_and_hash_matches():
    entry = ManifestEntry(url=f"{BASE}/a", slug="a", categories=["Career Growth"])
    rec = build_record(entry, sample_extracted(), "2026-10-31T11:00:00Z", 300)
    validate_post(rec)
    assert rec["content_hash"] == hashlib.sha256(rec["text"].encode()).hexdigest()
    assert list(rec) == [
        "schema_version", "id", "url", "title", "subtitle", "author", "published_date", "categories",
        "word_count", "summary", "sections", "text", "links", "content_hash", "fetched_at", "status",
    ]
    assert rec["status"] == "ok"


@pytest.mark.parametrize(
    "words, paywalled, status",
    [(120, False, "partial"), (2140, False, "ok"), (299, False, "partial"), (300, False, "ok"), (700, True, "partial")],
)
def test_status_from_word_count_and_paywall(words, paywalled, status):
    entry = ManifestEntry(url=f"{BASE}/a", slug="a")
    rec = build_record(entry, sample_extracted(words, paywalled), "2026-10-31T11:00:00Z", 300)
    assert rec["status"] == status and rec["word_count"] == words


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(extra=1),
        lambda r: r.pop("summary"),
        lambda r: r.update(published_date="Oct 26"),
        lambda r: r.update(status="failed"),
        lambda r: r.update(content_hash="abc"),
        lambda r: r["sections"].append({"heading": None, "level": 9, "text": "x"}),
        lambda r: r.update(schema_version=2),
    ],
)
def test_schema_rejects_bad_records(mutate):
    rec = build_record(ManifestEntry(url=f"{BASE}/a", slug="a"), sample_extracted(), "2026-10-31T11:00:00Z", 300)
    mutate(rec)
    with pytest.raises(ExtractError, match="schema"):
        validate_post(rec)


def test_run_extract_writes_valid_posts(cfg):
    m = seed(cfg, ["a", "b"], categories=["Career Growth"])
    result = run_extract(cfg, m)
    assert (result.ok, result.written, result.failed) == (2, 2, 0)
    rec = json.loads((cfg.posts_dir / "a.json").read_text())
    validate_post(rec)
    assert rec["id"] == "a" and rec["categories"] == ["Career Growth"]
    assert rec["fetched_at"] == store.mtime_iso(cfg.raw_dir / "a.html")
    saved = load_manifest(cfg.manifest_path).entries["a"]
    assert saved.status == "ok" and saved.content_hash == rec["content_hash"]


# spec: post-storage / Unchanged post
def test_unchanged_post_not_rewritten(cfg):
    m = seed(cfg, ["a"])
    run_extract(cfg, m)
    path = cfg.posts_dir / "a.json"
    os.utime(path, (1_000_000, 1_000_000))
    result = run_extract(cfg, m)
    assert result.unchanged == 1 and result.written == 0
    assert path.stat().st_mtime == 1_000_000


# spec: post-storage / Changed post
def test_changed_text_rewrites(cfg):
    m = seed(cfg, ["a"])
    run_extract(cfg, m)
    old_hash = m.entries["a"].content_hash
    html = post_page("a").replace("Good intentions.", "Brand new heading")
    html = html.replace("Meeting idea outcome", "Completely different opening", 1)
    write_text_atomic(cfg.raw_dir / "a.html", html)
    result = run_extract(cfg, m)
    assert result.written == 1
    assert m.entries["a"].content_hash != old_hash
    assert json.loads((cfg.posts_dir / "a.json").read_text())["content_hash"] == m.entries["a"].content_hash


# spec: post-storage / Categories changed, text unchanged
def test_category_change_rewrites(cfg):
    m = seed(cfg, ["a"], categories=["One"])
    run_extract(cfg, m)
    m.entries["a"].categories = ["One", "Two"]
    assert run_extract(cfg, m).written == 1
    assert json.loads((cfg.posts_dir / "a.json").read_text())["categories"] == ["One", "Two"]


def test_refresh_rewrites_even_if_unchanged(cfg):
    m = seed(cfg, ["a"])
    run_extract(cfg, m)
    assert run_extract(cfg, m, refresh=True).written == 1


def test_extract_error_marks_failed_and_continues(cfg):
    m = seed(cfg, ["good"])
    m.entries["bad"] = ManifestEntry(url=f"{BASE}/bad", slug="bad", status="fetched")
    write_text_atomic(cfg.raw_dir / "bad.html", "<html><body>nothing here</body></html>")
    result = run_extract(cfg, m)
    assert (result.ok, result.failed) == (1, 1)
    assert m.entries["bad"].status == "failed" and m.entries["bad"].error.startswith("extract:")
    assert not (cfg.posts_dir / "bad.json").exists()


def test_unexpected_exception_is_contained(cfg, monkeypatch):
    m = seed(cfg, ["a", "b"])
    calls = {"n": 0}
    real = store.extract_post

    def flaky(html, url):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("parser exploded")
        return real(html, url)

    monkeypatch.setattr(store, "extract_post", flaky)
    result = run_extract(cfg, m)
    assert result.failed == 1 and result.ok == 1
    assert "RuntimeError" in m.entries["a"].error


def test_posts_without_raw_are_skipped(cfg):
    m = seed(cfg, ["a"])
    m.entries["nofile"] = ManifestEntry(url=f"{BASE}/nofile", slug="nofile")
    run_extract(cfg, m)
    assert m.entries["nofile"].status == "discovered"


def test_limit_applies_to_first_n(cfg):
    m = seed(cfg, ["a", "b", "c"])
    assert run_extract(cfg, m, limit=2).written == 2
    assert not (cfg.posts_dir / "c.json").exists()


def test_dry_run_writes_nothing(cfg):
    m = seed(cfg, ["a"])
    result = run_extract(cfg, m, dry_run=True)
    assert result.would_write == ["a"]
    assert not cfg.posts_dir.exists() and not cfg.manifest_path.exists()


def test_partial_for_paywalled_page(cfg):
    m = seed(cfg, ["a"], html=fixture_text("post-signed-out.html"))
    result = run_extract(cfg, m)
    assert result.partial == 1
    assert json.loads((cfg.posts_dir / "a.json").read_text())["status"] == "partial"


# spec: content-extraction / Extract without network
def test_extract_needs_no_network(cfg, monkeypatch):
    import socket

    def no_net(*a, **k):
        raise AssertionError("network used during extract")

    monkeypatch.setattr(socket.socket, "connect", no_net)
    m = seed(cfg, ["a"])
    assert run_extract(cfg, m).written == 1

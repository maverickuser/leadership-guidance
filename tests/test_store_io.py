import json
import os

import pytest

from scarlet_archive.errors import ArchiveError
from scarlet_archive.models import Manifest, ManifestEntry
from scarlet_archive.store import load_manifest, save_manifest, write_json_atomic, write_text_atomic


def test_manifest_round_trip_keeps_order_and_fields(tmp_path):
    m = Manifest()
    for slug in ["zeta", "alpha", "mid"]:
        m.entries[slug] = ManifestEntry(url=f"https://x/{slug}", slug=slug, categories=["A"])
    m.entries["alpha"].status = "failed"
    m.entries["alpha"].error = "HTTP 404"
    m.entries["alpha"].last_attempt = "2026-10-31T11:00:00Z"
    path = tmp_path / "data" / "manifest.json"
    save_manifest(m, path)
    loaded = load_manifest(path)
    assert [e.slug for e in loaded.ordered()] == ["zeta", "alpha", "mid"]
    assert loaded.entries["alpha"] == m.entries["alpha"]
    raw = json.loads(path.read_text())
    assert raw["version"] == 1 and raw["updated_at"].endswith("Z")
    assert set(raw["posts"][0]) == {"url", "slug", "categories", "status", "last_attempt", "error", "content_hash"}


def test_missing_manifest_is_empty(tmp_path):
    assert len(load_manifest(tmp_path / "nope.json")) == 0


def test_corrupt_manifest_gives_clear_error(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text("{not json")
    with pytest.raises(ArchiveError, match="not valid JSON"):
        load_manifest(p)


def test_unknown_status_falls_back_to_discovered(tmp_path):
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({"posts": [{"url": "u", "slug": "s", "status": "weird"}]}))
    assert load_manifest(p).entries["s"].status == "discovered"


def test_atomic_write_keeps_old_file_when_replace_fails(tmp_path, monkeypatch):
    target = tmp_path / "post.json"
    write_json_atomic(target, {"v": 1})

    def broken_replace(src, dst):
        raise OSError("killed mid-write")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        write_json_atomic(target, {"v": 2})
    assert json.loads(target.read_text()) == {"v": 1}


def test_atomic_write_new_file_absent_when_replace_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(OSError):
        write_text_atomic(tmp_path / "new.html", "<html>")
    assert not (tmp_path / "new.html").exists()


def test_leftover_tmp_does_not_break_next_write(tmp_path):
    (tmp_path / "a.json.tmp").write_text("garbage")
    write_json_atomic(tmp_path / "a.json", {"ok": True})
    assert json.loads((tmp_path / "a.json").read_text()) == {"ok": True}
    assert not (tmp_path / "a.json.tmp").exists()


def test_unicode_preserved(tmp_path):
    write_json_atomic(tmp_path / "u.json", {"t": "“best intentions” – café"})
    text = (tmp_path / "u.json").read_text(encoding="utf-8")
    assert "“best intentions” – café" in text and text.endswith("\n")

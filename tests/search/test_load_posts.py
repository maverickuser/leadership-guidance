"""`load_posts` reads every newsletter's folder without the database."""

from __future__ import annotations

import json

import pytest

from newsletter_archive.search.indexer import load_posts

from .archive import record, write_archive


@pytest.mark.parametrize("content", ["[1, 2]", '"text"', "null"])
def test_a_post_file_that_is_not_a_record_fails_alone(cfg, content):
    write_archive(cfg, [record("alpha", 1), record("beta", 2)])
    cfg.only_source().post_path("beta").write_text(content, encoding="utf-8")
    posts, failed, _keep = load_posts(cfg)
    assert list(posts) == ["main/alpha"]
    assert failed == {"main/beta": "ArchiveError: not a post record"}


def test_an_old_layout_post_file_asks_for_migrate(cfg):
    write_archive(cfg, [record("alpha", 1)])
    old = record("alpha", 1) | {"schema_version": 1, "id": "alpha"}
    del old["source"]
    cfg.only_source().post_path("alpha").write_text(json.dumps(old), encoding="utf-8")
    _posts, failed, _keep = load_posts(cfg)
    assert "run `archive migrate`" in failed["main/alpha"]

"""Build a small archive (manifest + post files) for indexer tests. Filler text only."""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import replace
from pathlib import Path
from typing import Any

from newsletter_archive.config import Config
from newsletter_archive.models import Manifest, ManifestEntry
from newsletter_archive.store import save_manifest

from .textgen import section_text

INTRO = (
    "Welcome to the example letter, I'm a director of engineering who has spent many years building "
    "teams and growing leaders at large companies. Each week I write about careers, hiring, promotions, "
    "feedback and the habits that help managers and engineers do their best work. Thanks for being "
    "here, and please share it with a friend who might enjoy it too."
)


def record(
    slug: str, seed: int, *, title: str | None = None, intro: bool = True, sizes=(120, 160, 90)
) -> dict[str, Any]:
    rng = random.Random(seed)
    sections = [
        {"heading": None, "level": 0, "text": (INTRO + "\n\n" if intro else "") + section_text(rng, [60])},
        *[
            {"heading": f"Part {i}", "level": 2, "text": section_text(rng, [s, 40])}
            for i, s in enumerate(sizes)
        ],
    ]
    text = "\n\n".join(s["text"] for s in sections)
    return {
        "schema_version": 1,
        "id": slug,
        "url": f"https://www.example.com/{slug}/",
        "title": title or f"Post {slug}",
        "subtitle": None,
        "author": "Author",
        "published_date": f"2025-01-{1 + seed % 28:02d}",
        "categories": ["Leadership"] if seed % 2 else ["Career"],
        "word_count": len(text.split()),
        "summary": "Short description.",
        "sections": sections,
        "text": text,
        "links": [],
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "fetched_at": "2026-10-31T00:00:00Z",
        "status": "ok",
    }


def write_archive(config: Config, records: list[dict[str, Any]], extra_manifest: list[str] = ()) -> None:
    config.posts_dir.mkdir(parents=True, exist_ok=True)
    entries = {}
    for r in records:
        config.post_path(r["id"]).write_text(json.dumps(r), encoding="utf-8")
        entries[r["id"]] = ManifestEntry(url=r["url"], slug=r["id"], categories=r["categories"], status="ok")
    for slug in extra_manifest:
        entries[slug] = ManifestEntry(url=f"https://www.example.com/{slug}/", slug=slug, status="ok")
    save_manifest(Manifest(entries=entries), config.manifest_path)


def small_config(config: Config, **search) -> Config:
    s = replace(config.search, embedding_model="fake-model", embedding_dimension=8, **search)
    return replace(config, search=s)


def remove_post(config: Config, slug: str) -> None:
    path = Path(config.post_path(slug))
    path.unlink()
    data = json.loads(config.manifest_path.read_text())
    data["posts"] = [p for p in data["posts"] if p["slug"] != slug]
    config.manifest_path.write_text(json.dumps(data))

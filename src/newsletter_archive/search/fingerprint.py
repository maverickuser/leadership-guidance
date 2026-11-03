"""Post fingerprint: a hash of everything that shapes a post's index entries (pure).

When the fingerprint changes, the post is re-indexed; when it doesn't, nothing is done. It
covers the stored post fields, the cleaned text (so a newly detected boilerplate paragraph
changes it), chunk settings and the versions of every step that writes to the index.
``fetched_at`` is left out: re-fetching an unchanged page shouldn't re-index it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from ..config import SearchConfig
from .models import Post, Section
from .versions import CHUNKER_VERSION, EMBED_INPUT_VERSION, SUMMARY_PROMPT_VERSION


def fingerprint_payload(
    post: Post, clean_sections: Sequence[Section], search: SearchConfig
) -> dict[str, Any]:
    summaries = search.summaries
    return {
        "post_id": post.id,
        "title": post.title,
        "subtitle": post.subtitle,
        "author": post.author,
        "url": post.url,
        "published_date": post.published_date,
        "categories": sorted(post.categories),
        "status": post.status,
        "word_count": post.word_count,
        "sections": [[s.heading, s.text] for s in clean_sections],
        "chunker_version": CHUNKER_VERSION,
        "chunk_words": search.chunk_words,
        "chunk_overlap_words": search.chunk_overlap_words,
        "embed_input_version": EMBED_INPUT_VERSION,
        "summaries": (
            {
                "model": summaries.model,
                "thinking_level": summaries.thinking_level,
                "prompt_version": SUMMARY_PROMPT_VERSION,
            }
            if summaries.enabled
            else None
        ),
    }


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def post_fingerprint(post: Post, clean_sections: Sequence[Section], search: SearchConfig) -> str:
    payload = canonical_json(fingerprint_payload(post, clean_sections, search))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

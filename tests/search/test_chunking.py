import hashlib
import itertools
import json
import os
import random
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from newsletter_archive.search import chunking
from newsletter_archive.search.chunking import Chunk, chunk_sections, merge_small_sections
from newsletter_archive.search.models import Section
from newsletter_archive.search.text import words
from newsletter_archive.search.versions import CHUNKER_VERSION

from .textgen import WORDS, paragraph, section_text

GOLDEN = Path(__file__).parent / "golden" / "chunks.json"


def _all_words(sections):
    return [w for s in merge_small_sections(sections) for w in words(s.text)]


def _new_words(chunks: list[Chunk]):
    return [w for c in chunks for w in words(c.text)[c.overlap_words :]]


# spec: search-indexing / Passages / Long section
def test_long_section_becomes_three_overlapping_passages():
    rng = random.Random(1)
    sec = Section("Big", section_text(rng, [50] * 14))  # 700 words, 10-word sentences
    chunks = chunk_sections([sec], 300, 50)
    assert len(chunks) == 3
    assert all(c.word_count <= 300 for c in chunks)
    assert chunks[0].overlap_words == 0 and all(0 < c.overlap_words <= 50 for c in chunks[1:])
    for prev, cur in itertools.pairwise(chunks):
        assert words(prev.text)[-cur.overlap_words :] == words(cur.text)[: cur.overlap_words]
    assert _new_words(chunks) == words(sec.text)


# spec: search-indexing / Passages / Tiny section
def test_tiny_section_merges_into_the_next():
    rng = random.Random(2)
    chunks = chunk_sections([Section("Intro", paragraph(rng, 12)), Section("Main", paragraph(rng, 200))])
    assert len(chunks) == 1 and chunks[0].heading == "Intro" and chunks[0].word_count == 212


def test_merge_rules():
    rng = random.Random(3)
    small, big = paragraph(rng, 10), paragraph(rng, 60)
    # trailing small section joins the previous one; untitled first section takes the next heading
    merged = merge_small_sections([Section("A", big), Section("B", small)])
    assert [s.heading for s in merged] == ["A"] and merged[0].text.endswith(small)
    assert merge_small_sections([Section(None, small), Section("B", big)])[0].heading == "B"
    assert merge_small_sections([Section("Only", small)]) == [Section("Only", small)]
    assert merge_small_sections([Section("Empty", "  "), Section("B", big)]) == [Section("B", big)]


# spec: search-indexing / Passages / Deterministic ids
def test_ids_are_sequential_and_deterministic():
    rng = random.Random(4)
    secs = [Section(f"H{i}", section_text(rng, [80, 120, 90])) for i in range(3)]
    first, second = chunk_sections(secs), chunk_sections(secs)
    assert first == second
    assert [c.n for c in first] == list(range(len(first)))


def test_very_long_sentence_is_split_by_words():
    rng = random.Random(5)
    sec = Section("Run-on", section_text(rng, [700], sentence_words=700))
    chunks = chunk_sections([sec], 300, 50)
    assert [c.word_count for c in chunks] == [300, 300, 100]
    assert all(c.overlap_words == 0 for c in chunks)  # no whole sentence fits as overlap


LIMIT = chunking.MAX_PASSAGE_CHARS


def test_token_guard_cuts_only_the_oversized_word():
    url = "https://example.com/" + "x" * 9000
    chunks = chunk_sections([Section("URL", " ".join(["word"] * 49) + " " + url)])
    assert [c.word_count for c in chunks] == [49, 1, 1]
    assert all(len(c.text) <= LIMIT for c in chunks)
    assert "".join(c.text for c in chunks[1:]) == url
    assert [c.n for c in chunks] == [0, 1, 2]


def test_token_guard_keeps_paragraph_breaks_and_packs_greedily():
    text = "para one " * 900 + "\n\n" + "para two " * 900
    parts = chunking._token_guard(text.strip())
    assert all(len(p) <= LIMIT for p in parts) and len(parts) == 3
    assert any("\n\n" in p for p in parts)
    assert chunking._token_guard("short") == ["short"]


def test_split_window_never_repeats_only_overlap():
    s1 = " ".join(["Alpha"] * 214) + " end."
    s2 = " ".join(["Beta"] * 44) + " end."
    url = "https://example.com/" + "x" * 9000
    chunks = chunk_sections([Section("B", f"{s1} {s2}\n\n{url} tail words here.")])
    assert all(c.overlap_words < c.word_count or c.overlap_words == 0 for c in chunks)
    assert all(c.overlap_words == 0 for c in chunks[1:])


def test_invalid_settings_rejected():
    for words_, overlap in ((0, 0), (100, 100), (100, -1)):
        with pytest.raises(ValueError, match="chunk_words"):
            chunk_sections([Section("A", "text")], words_, overlap)


_SECTION = st.builds(
    lambda heading, paras: Section(heading, "\n\n".join(paras)),
    st.one_of(st.none(), st.sampled_from(["Intro", "Why", "How"])),
    st.lists(
        st.lists(
            st.builds(
                lambda ws, end: " ".join([ws[0].capitalize(), *ws[1:]]) + end,
                st.lists(st.sampled_from(WORDS), min_size=1, max_size=60),
                st.sampled_from([".", "?", "!"]),
            ),
            min_size=1,
            max_size=8,
        ).map(" ".join),
        min_size=1,
        max_size=6,
    ),
)


@settings(max_examples=150, deadline=None)
@given(st.lists(_SECTION, max_size=6), st.integers(20, 300), st.data())
def test_chunking_properties(sections, chunk_words, data):
    overlap = data.draw(st.integers(0, chunk_words - 1))
    chunks = chunk_sections(sections, chunk_words, overlap)
    assert all(c.word_count <= chunk_words for c in chunks)
    assert all(c.overlap_words <= overlap for c in chunks)
    assert _new_words(chunks) == _all_words(sections)  # no word lost, none invented
    assert [c.n for c in chunks] == list(range(len(chunks)))
    assert chunk_sections(sections, chunk_words, overlap) == chunks


TRICKY = (
    "Ask Dr. Smith about it. We met the team at Acme Inc. Today we start. J. K. Rowling wrote "
    "a book, e.g. this one. Thanks for joining us. The answer is no. See No. 5 for details. "
    "Plan A. We moved on, i.e. quickly. Prices rose 2.5 percent in the U.S. Last year was hard."
)


def _golden_posts():
    rng = random.Random(20261101)
    posts = [
        [
            Section("Tricky", "\n\n".join([TRICKY] * 6)),
            Section("Link", "Read more at https://example.com/" + "y" * 8500 + " and then continue reading."),
        ]
    ]
    for p in range(4):
        secs = [Section(None, section_text(rng, [rng.randint(5, 40)]))]
        for h in range(rng.randint(2, 5)):
            sizes = [rng.randint(15, 160) for _ in range(rng.randint(1, 6))]
            secs.append(
                Section(f"Heading {p}.{h}", section_text(rng, sizes, sentence_words=rng.randint(4, 30)))
            )
        posts.append(secs)
    return posts


def _golden_snapshot():
    return [
        [
            [c.n, c.heading, c.word_count, c.overlap_words, hashlib.sha256(c.text.encode()).hexdigest()[:16]]
            for c in chunk_sections(secs)
        ]
        for secs in _golden_posts()
    ]


def test_golden_snapshot_matches_chunker_version():
    snapshot = {"chunker_version": CHUNKER_VERSION, "posts": _golden_snapshot()}
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(snapshot, indent=1) + "\n", encoding="utf-8")
    stored = json.loads(GOLDEN.read_text(encoding="utf-8"))
    if stored["posts"] != snapshot["posts"]:
        assert stored["chunker_version"] != CHUNKER_VERSION, (
            "chunk output changed but CHUNKER_VERSION did not: bump it in search/versions.py, "
            "then regenerate with UPDATE_GOLDEN=1 pytest tests/search/test_chunking.py"
        )
        pytest.fail("CHUNKER_VERSION bumped: regenerate the golden file with UPDATE_GOLDEN=1")
    assert stored["chunker_version"] == CHUNKER_VERSION, "regenerate the golden file with UPDATE_GOLDEN=1"

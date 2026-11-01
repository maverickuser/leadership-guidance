"""Split a cleaned post into passages for search (pure; no I/O).

1. Sections under ``MIN_SECTION_WORDS`` are merged into the next section (or the previous
   one when last). The merged passage keeps the first section's heading.
2. Each section is packed greedily into windows of at most ``chunk_words`` words: whole
   paragraphs while they fit, then whole sentences, and a single over-long sentence is split
   by words.
3. Each window after the first starts with the previous window's trailing whole sentences,
   up to ``overlap_words`` words, so an idea cut at a boundary appears in both passages.
4. A passage over ``MAX_PASSAGE_TOKENS`` estimated tokens (characters / 4, e.g. long URLs) is
   split again by words, so nothing is ever truncated by the embedding API.

Any change to the output for the same input must bump ``CHUNKER_VERSION``
(``search/versions.py``); golden tests enforce this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Section
from .sentences import split_sentences
from .text import paragraphs, word_count, words

MIN_SECTION_WORDS = 40
MAX_PASSAGE_TOKENS = 2000
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class Chunk:
    """One passage. ``overlap_words`` leading words repeat the end of the previous passage."""

    n: int
    heading: str | None
    text: str
    word_count: int
    overlap_words: int = 0


@dataclass(frozen=True)
class _Piece:
    """A sentence (or a word-split part of a long one) inside a paragraph."""

    text: str
    words: int
    para: int


def merge_small_sections(sections: list[Section], min_words: int = MIN_SECTION_WORDS) -> list[Section]:
    """Merge sections under ``min_words`` into the following one (the previous one if last)."""
    out: list[Section] = []
    pending: Section | None = None
    for sec in sections:
        if not sec.text.strip():
            continue
        if pending is not None:
            sec = Section(
                pending.heading if pending.heading is not None else sec.heading,
                pending.text + "\n\n" + sec.text,
            )
            pending = None
        if word_count(sec.text) < min_words:
            pending = sec
        else:
            out.append(sec)
    if pending is not None:
        if out:
            last = out.pop()
            out.append(Section(last.heading, last.text + "\n\n" + pending.text))
        else:
            out.append(pending)
    return out


def _pieces(section_text: str, limit: int) -> list[list[_Piece]]:
    """Paragraphs of a section as lists of pieces, each piece at most ``limit`` words."""
    out: list[list[_Piece]] = []
    for p_index, para in enumerate(paragraphs(section_text)):
        para_pieces: list[_Piece] = []
        for sentence in split_sentences(para):
            ws = words(sentence)
            for i in range(0, len(ws), limit):
                part = ws[i : i + limit]
                para_pieces.append(_Piece(" ".join(part), len(part), p_index))
        out.append(para_pieces)
    return out


def _render(pieces: list[_Piece]) -> str:
    """Join pieces: a space inside a paragraph, a blank line between paragraphs."""
    text = ""
    for i, piece in enumerate(pieces):
        if i:
            text += "\n\n" if piece.para != pieces[i - 1].para else " "
        text += piece.text
    return text


def _overlap_tail(window: list[_Piece], overlap_words: int) -> list[_Piece]:
    """Trailing whole pieces of a window totalling at most ``overlap_words`` words."""
    tail: list[_Piece] = []
    total = 0
    for piece in reversed(window):
        if total + piece.words > overlap_words:
            break
        tail.insert(0, piece)
        total += piece.words
    return tail


def _pack(section_text: str, limit: int, overlap_words: int) -> list[tuple[list[_Piece], int]]:
    """Greedy windows for one section, as (pieces, number of leading overlap pieces)."""
    windows: list[tuple[list[_Piece], int]] = []
    cur: list[_Piece] = []
    cur_words = 0
    overlap_count = 0

    def close() -> None:
        nonlocal cur, cur_words, overlap_count
        windows.append((cur, overlap_count))
        tail = _overlap_tail(cur, overlap_words)
        cur, cur_words, overlap_count = list(tail), sum(p.words for p in tail), len(tail)

    for para_pieces in _pieces(section_text, limit):
        para_words = sum(p.words for p in para_pieces)
        if cur_words + para_words <= limit:
            cur.extend(para_pieces)
            cur_words += para_words
            continue
        for piece in para_pieces:
            if cur_words + piece.words > limit and len(cur) > overlap_count:
                close()
            if cur_words + piece.words > limit:
                # the overlap plus this piece won't fit: start the window without overlap
                cur, cur_words, overlap_count = [], 0, 0
            cur.append(piece)
            cur_words += piece.words
    if len(cur) > overlap_count:
        windows.append((cur, overlap_count))
    return windows


_TOKEN_RE = re.compile(r"\S+|\s+")


def _token_guard(text: str, max_tokens: int = MAX_PASSAGE_TOKENS) -> list[str]:
    """Split text that is over the token estimate (e.g. a pasted long URL): words are packed
    greedily up to the character limit, only a word that is itself over the limit is cut, and
    whitespace (including paragraph breaks) inside each part is kept."""
    limit = max_tokens * CHARS_PER_TOKEN
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    cur = ""
    for token in _TOKEN_RE.findall(text):
        pieces = [token] if len(token) <= limit or token.isspace() else [
            token[i : i + limit] for i in range(0, len(token), limit)
        ]  # fmt: skip
        for piece in pieces:
            if len(cur) + len(piece) > limit and cur.strip():
                parts.append(cur.strip())
                cur = ""
            cur += piece
    if cur.strip():
        parts.append(cur.strip())
    return parts


def chunk_sections(sections: list[Section], chunk_words: int = 300, overlap_words: int = 50) -> list[Chunk]:
    """Passages for one cleaned post, numbered from 0 in reading order."""
    if chunk_words <= 0 or not 0 <= overlap_words < chunk_words:
        raise ValueError("need chunk_words > 0 and 0 <= overlap_words < chunk_words")
    chunks: list[Chunk] = []
    for sec in merge_small_sections(sections):
        for pieces, overlap_count in _pack(sec.text, chunk_words, overlap_words):
            text = _render(pieces)
            overlap = sum(p.words for p in pieces[:overlap_count])
            parts = _token_guard(text)
            if len(parts) > 1 and overlap_count:
                # Splitting an oversized window: drop its overlap so no part is only a repeat.
                parts = _token_guard(_render(pieces[overlap_count:]))
                overlap = 0
            for i, part in enumerate(parts):
                chunks.append(
                    Chunk(
                        n=len(chunks),
                        heading=sec.heading,
                        text=part,
                        word_count=word_count(part),
                        overlap_words=overlap if i == 0 else 0,
                    )
                )
    return chunks

"""Small text helpers shared by boilerplate detection, chunking and keyword search."""

from __future__ import annotations

import math
import re
import unicodedata

CHARS_PER_TOKEN = 4  # rough English average: for pacing and cost estimates only
# Hard cap on one embedding input. The model takes 8,192 tokens and truncates silently beyond
# that; 8,000 characters stays under the limit even at one character per token.
MAX_EMBED_CHARS = 8000
# Room left in each embedding input for "title: … | text: heading\n" around the passage body.
EMBED_PREFIX_CHARS = 1000
MAX_PASSAGE_CHARS = MAX_EMBED_CHARS - EMBED_PREFIX_CHARS

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_PARA_SPLIT_RE = re.compile(r"\n\s*\n")
# Letters that NFKD does not decompose into a base letter + accent.
_EXTRA_FOLD = str.maketrans({"ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ß": "ss",
                             "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ı": "i"})  # fmt: skip


def fold(text: str) -> str:
    """Remove accents ("Résumé" -> "Resume"). Used for the BM25 columns and for queries, because
    pg_search stems words before folding accents, which would index "résumé" and "resume"
    differently."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_EXTRA_FOLD))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def words(text: str) -> list[str]:
    return text.split()


def word_count(text: str) -> int:
    return len(text.split())


def squash(text: str) -> str:
    """Collapse all whitespace runs to single spaces."""
    return " ".join(text.split())


def paragraphs(text: str) -> list[str]:
    """Paragraphs of a section (blank-line separated), whitespace-squashed, empties dropped."""
    return [p for p in (squash(part) for part in _PARA_SPLIT_RE.split(text)) if p]


def normalize_for_matching(text: str) -> str:
    """Lowercase, accent-free, punctuation-free, single-spaced: for duplicate detection."""
    return squash(_PUNCT_RE.sub(" ", fold(text).lower()))


def estimate_tokens(text: str) -> int:
    """Rough token count for pacing and cost; never used to decide whether text fits."""
    return max(1, math.ceil(len(text) / CHARS_PER_TOKEN))

"""Sentence splitter, kept in this repo so library upgrades can't change passage boundaries.

A boundary is end punctuation (. ! ? or …), optional closing quotes or brackets, whitespace,
then something that can start a sentence (a capital, a digit, or an opening quote/bracket).
It is not a boundary after a known abbreviation ("Dr.", "e.g.", "U.S.") or a single initial
("J. K. Rowling"). Decimals ("2.5") never match because the period isn't followed by a space.
"""

from __future__ import annotations

import re

from .text import squash

# fmt: off
ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "vs", "etc", "al", "eg", "ie", "cf",
    "approx", "est", "fig", "no", "nos", "vol", "inc", "ltd", "co", "corp", "llc", "dept", "univ",
    "assn", "bros", "gen", "gov", "sen", "rep", "rev", "hon", "pres", "sgt", "capt", "col", "lt",
    "cmdr", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "mon", "tue", "tues", "wed", "thu", "thurs", "fri", "sat", "sun", "us", "uk", "eu", "un", "ph",
    "phd", "mba", "ba", "bsc", "msc", "ceo", "cto", "vp", "svp", "evp",
})
# fmt: on

_BOUNDARY_RE = re.compile(r"""([.!?…]+["'”’)\]]*)\s+(?=["'“‘(\[]?[A-Z0-9])""")
_LAST_TOKEN_RE = re.compile(r"(\S+)$")


def _is_abbreviation(before: str) -> bool:
    """`before` is the text up to (not including) the end punctuation."""
    m = _LAST_TOKEN_RE.search(before)
    if not m:
        return False
    token = m.group(1).lstrip("\"'“‘([")
    if len(token) == 1 and token.isalpha() and token.isupper():
        return True  # an initial: "J. K. Rowling"
    return token.replace(".", "").lower() in ABBREVIATIONS


def split_sentences(paragraph: str) -> list[str]:
    """Split one paragraph into sentences. Joining the result with single spaces gives back
    the whitespace-squashed paragraph exactly."""
    text = squash(paragraph)
    if not text:
        return []
    out: list[str] = []
    start = 0
    for m in _BOUNDARY_RE.finditer(text):
        punct_end = m.end(1)
        single_period = m.group(1).startswith(".") and len(m.group(1).rstrip("\"'”’)]")) == 1
        if single_period and _is_abbreviation(text[start : m.start(1)]):
            continue
        out.append(text[start:punct_end])
        start = m.end()
    out.append(text[start:])
    return out

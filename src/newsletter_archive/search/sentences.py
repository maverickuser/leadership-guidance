"""Sentence splitter, kept in this repo so library upgrades can't change passage boundaries.

A boundary is end punctuation (. ! ? or …), optional closing quotes or brackets, whitespace,
then something that can start a sentence (a capital, a digit, or an opening quote/bracket).
A single period is not a boundary after:
- a title or company abbreviation in its capitalised form ("Dr.", "Inc.", "Jan."),
- a lowercase Latin abbreviation ("etc.", "vs.", "cf."),
- a dotted abbreviation ("e.g.", "U.S.", "Ph.D."),
- "No." when a number follows ("No. 5"),
- an initial ("J. K. Rowling"), except "I." and "A.", which usually end a sentence.
Ordinary words that happen to be abbreviations elsewhere ("us", "no", "sun") always end a
sentence. Decimals ("2.5") never match because the period isn't followed by a space.
"""

from __future__ import annotations

import re

from .text import squash

# Matched case-sensitively as written.
CAPITALISED = frozenset({
    "Mr", "Mrs", "Ms", "Dr", "Prof", "Sr", "Jr", "St", "Mt", "Inc", "Ltd", "Co", "Corp", "Bros",
    "Dept", "Univ", "Gov", "Sen", "Rep", "Rev", "Hon", "Pres", "Sgt", "Capt", "Col", "Lt", "Cmdr",
    "Gen", "Jan", "Feb", "Mar", "Apr", "Jun", "Jul", "Aug", "Sep", "Sept", "Oct", "Nov", "Dec",
    "Fig", "Vol", "Approx", "Est",
})  # fmt: skip
LOWERCASE = frozenset({"etc", "vs", "cf", "al", "approx", "fig", "vol", "esp", "ca"})
NOT_INITIALS = frozenset({"I", "A"})

_BOUNDARY_RE = re.compile(r"""([.!?…]+["'”’)\]]*)\s+(?=["'“‘(\[]?[A-Z0-9])""")
_LAST_TOKEN_RE = re.compile(r"(\S+)$")


def _is_abbreviation(before: str, after: str) -> bool:
    """``before`` is the text up to (not including) the period; ``after`` follows the space."""
    m = _LAST_TOKEN_RE.search(before)
    if not m:
        return False
    token = m.group(1).lstrip("\"'“‘([")
    if "." in token:  # e.g / i.e / U.S / Ph.D
        return True
    if len(token) == 1 and token.isalpha() and token.isupper():
        return token not in NOT_INITIALS
    if token == "No":
        return after[:1].isdigit()
    return token in CAPITALISED or token in LOWERCASE


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
        if single_period and _is_abbreviation(text[start : m.start(1)], text[m.end() :]):
            continue
        out.append(text[start:punct_end])
        start = m.end()
    out.append(text[start:])
    return out

"""Version numbers for every step whose output is stored in the index.

Bump a version whenever that step's output changes for the same input. Each one is part of
the post fingerprint, so a bump re-indexes exactly the posts it affects.
"""

from __future__ import annotations

CHUNKER_VERSION = 2  # search/chunking.py and search/sentences.py (2: 7,000-character passage cap)
EMBED_INPUT_VERSION = 1  # the text sent to the embedding model for passages and summaries
SUMMARY_PROMPT_VERSION = 1  # the prompt that writes post summaries

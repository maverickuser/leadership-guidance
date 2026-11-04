"""Evaluate search quality on your own questions: recall@10 and MRR per stage.

Run: python scripts/eval_search.py [--config config.yaml] [--generation N] [--rerank-top N]
Questions: <output_dir>/eval-queries.yaml (personal, never committed). See lld.md §13.
Calls the Gemini API once per question and variant (query embeddings only; a fraction of a cent).
"""

import sys

from newsletter_archive.search.evaluate import main

if __name__ == "__main__":
    sys.exit(main())

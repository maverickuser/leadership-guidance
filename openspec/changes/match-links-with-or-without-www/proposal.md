## Why

The answer eval counts a link as pointing to a returned post only when its URL matches the post's stored URL. The match ignores case, a trailing slash, the query and the fragment, but not a leading `www.`. In the `coach-voice` run (2026-11-15), 3 of the 36 answers linked the right Scarlet Ink posts as `scarletink.com/...` instead of `www.scarletink.com/...`. Those links counted as citations to posts never returned. Citation precision showed 0.94 instead of 1.00, and post coverage 0.65 instead of 0.70. An offline recount that ignores `www.` gives the same scores as the old voice.

## What Changes

- **Link matching ignores a leading `www.`** in the host. `https://scarletink.com/a` and `https://www.scarletink.com/a` are the same post.
- **Computed scores are refreshed on reuse.** When a saved grade is reused, citation precision and post coverage are recomputed from the answer with the current matching. Today only post coverage is recomputed. Re-running a saved label then picks up the fix at no Gemini cost, and the judge's claims and overall grade are kept.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `answer-evaluation`: the Grade each answer requirement gains a scenario for links with or without `www.`, and the Saved answers and grades requirement says computed scores are recomputed when a grade is reused.

## Impact

- `src/newsletter_archive/search/answers.py`: `normalize_url` and the reuse branch of `grade_answers`.
- `tests/search/test_answers.py`: matching with and without `www.`, and recomputing on reuse.
- No Gemini spend. Afterwards, re-running `--answers --label coach-voice` and `--label posts-max-10x3` only searches locally and recounts.
- Not affected: `discover.normalize_url`, a separate function that keeps `www.` because crawling needs the exact host.

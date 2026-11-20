from dataclasses import replace

from newsletter_archive.config import SearchConfig, SummaryConfig
from newsletter_archive.search.fingerprint import canonical_json, post_fingerprint
from newsletter_archive.search.models import Post, Section

RECORD = {
    "id": "a-post",
    "title": "A post",
    "url": "https://example.com/a-post/",
    "status": "ok",
    "sections": [
        {"heading": None, "level": 0, "text": "Intro."},
        {"heading": "H", "level": 2, "text": "Body."},
    ],
    "subtitle": None,
    "author": "Author",
    "published_date": "2025-01-02",
    "categories": ["B", "A"],
    "word_count": 2,
    "fetched_at": "2026-10-31T00:00:00Z",
}
POST = Post.from_record(RECORD)
SECTIONS = list(POST.sections)
SEARCH = SearchConfig(
    embedding_model="gemini-embedding-2", embedding_dimension=3072
)  # the live index's model


def fp(post=POST, sections=SECTIONS, search=SEARCH):
    return post_fingerprint(post, sections, search)


def test_post_from_record():
    assert POST.sections == (Section(None, "Intro."), Section("H", "Body."))
    assert POST.categories == ("B", "A") and POST.word_count == 2 and POST.fetched_at


def test_stable_and_ignores_fetch_time_and_category_order():
    assert fp() == fp()
    assert fp(replace(POST, fetched_at="2027-01-01T00:00:00Z")) == fp()
    assert fp(replace(POST, categories=("A", "B"))) == fp()


def test_every_index_input_changes_it():
    base = fp()
    changed = [
        fp(replace(POST, title="New title")),
        fp(replace(POST, url="https://example.com/moved/")),
        fp(replace(POST, published_date="2025-01-03")),
        fp(replace(POST, categories=("A",))),
        fp(replace(POST, status="partial")),
        fp(replace(POST, author="Other")),
        fp(sections=[Section("H", "Body.")]),  # e.g. a paragraph became boilerplate
        fp(search=replace(SEARCH, chunk_words=400)),
        fp(search=replace(SEARCH, chunk_overlap_words=60)),
        fp(search=replace(SEARCH, summaries=SummaryConfig(model="other"))),
        fp(search=replace(SEARCH, summaries=SummaryConfig(enabled=False))),
    ]
    assert base not in changed and len(set(changed)) == len(changed)


def test_embedding_model_is_not_part_of_it():
    # a new model or dimension needs a new generation instead (lld.md 5.5)
    assert fp(search=replace(SEARCH, embedding_model="other", embedding_dimension=768)) == fp()


def test_canonical_json():
    assert canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'


# --- per-model input format (use-local-embeddings) ---------------------------------------------

LOCAL = "local:qwen3-embedding-0.6b-archive-v5"


def test_gemini_fingerprint_unchanged():
    """The Gemini payload must stay byte-identical, or the live index would be re-embedded."""
    from newsletter_archive.search.fingerprint import fingerprint_payload

    payload = fingerprint_payload(POST, SECTIONS, SEARCH)
    assert payload["embed_input_version"] == 1 and isinstance(payload["embed_input_version"], int)
    assert post_fingerprint(POST, SECTIONS, SEARCH, "gemini-embedding-2") == fp()
    # pinned from main before this change: a different value would re-index every live post
    assert fp() == "3ccce9510f60c03389953150a36068f83db48e69dd164dcee5ec65f5bd3adf7c"


def test_local_model_changes_the_fingerprint():
    local = post_fingerprint(POST, SECTIONS, SEARCH, LOCAL)
    assert local != fp()
    assert post_fingerprint(POST, SECTIONS, replace(SEARCH, embedding_model=LOCAL)) == local


def test_model_argument_overrides_the_configured_model():
    """A run writing the live Gemini version keeps Gemini fingerprints while the config names a local model."""
    configured_local = replace(SEARCH, embedding_model=LOCAL)
    assert post_fingerprint(POST, SECTIONS, configured_local, "gemini-embedding-2") == fp()

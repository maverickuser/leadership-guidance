from newsletter_archive.search.models import Post
from newsletter_archive.search.planner import (
    MAX_ATTEMPTS,
    Estimate,
    Failure,
    Prepared,
    Stored,
    make_plan,
    mass_delete_limit,
)


def prep(pid, fp="fp"):
    return Prepared(Post(pid, pid, f"https://example.com/{pid}", "ok", ()), [], fp)


def test_new_changed_unchanged_removed():
    prepared = {"a": prep("a"), "b": prep("b", "new-fp"), "c": prep("c")}
    stored = {"b": Stored("old-fp", True), "c": Stored("fp", True), "gone": Stored("fp", True)}
    plan = make_plan(prepared, stored, {})
    assert (plan.new, plan.changed, plan.unchanged, plan.removed) == (["a"], ["b"], ["c"], ["gone"])
    assert plan.to_index == ["a", "b"] and plan.guarded == []


# spec: search-indexing / Deletion guard / Truncated manifest
def test_deletion_guard():
    stored = {f"p{i}": Stored("fp", True) for i in range(252)}
    prepared = {f"p{i}": prep(f"p{i}") for i in range(20)}
    plan = make_plan(prepared, stored, {})
    assert plan.removed == [] and len(plan.guarded) == 232
    assert len(make_plan(prepared, stored, {}, allow_mass_delete=True).removed) == 232
    assert mass_delete_limit(252, 5, 0.10) == 25 and mass_delete_limit(20, 5, 0.10) == 5
    few = make_plan({f"p{i}": prep(f"p{i}") for i in range(247)}, stored, {})
    assert len(few.removed) == 5  # at the limit: allowed


def test_posts_that_failed_to_load_are_not_removed():
    plan = make_plan({}, {"broken": Stored("fp", True)}, {}, keep={"broken"})
    assert plan.removed == [] and plan.guarded == []


def test_gave_up_after_max_attempts_until_content_changes():
    failures = {"a": [Failure("embed", MAX_ATTEMPTS, "fp")]}
    assert make_plan({"a": prep("a")}, {}, failures).skipped == ["a"]
    assert make_plan({"a": prep("a", "edited")}, {}, failures).new == ["a"]
    assert make_plan({"a": prep("a")}, {}, failures, retry_failed=True).new == ["a"]
    fewer = {"a": [Failure("embed", MAX_ATTEMPTS - 1, "fp")]}
    assert make_plan({"a": prep("a")}, {}, fewer).new == ["a"]


def test_missing_summary_only_needs_the_summary():
    stored = {"a": Stored("fp", False)}
    assert make_plan({"a": prep("a")}, stored, {}).summary_only == ["a"]
    assert make_plan({"a": prep("a")}, stored, {}, summaries_enabled=False).summary_only == []
    gave_up = {"a": [Failure("summarize", MAX_ATTEMPTS, "fp")]}
    assert make_plan({"a": prep("a")}, stored, gave_up).summary_only == []
    assert make_plan({"a": prep("a")}, stored, gave_up, retry_failed=True).summary_only == ["a"]
    # a summary failure doesn't block re-indexing the passages
    assert make_plan({"a": prep("a", "edited")}, stored, gave_up).changed == ["a"]


def test_estimate():
    e = Estimate(10, 4, 2_000_000, 3, 1_000_000, 0.15, 0.50)
    assert round(e.cost_usd, 2) == 0.80
    assert "6 of 10 passages (4 cached)" in e.line() and "~$0.80" in e.line()

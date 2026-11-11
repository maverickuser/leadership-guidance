"""`archive migrate`: the one-time move of a single-newsletter archive into its own folder."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import newsletter_archive.migrate as migrate_module
from newsletter_archive.errors import ArchiveError, ConfigError
from newsletter_archive.migrate import migrate, prefix_eval_queries
from newsletter_archive.schema import validate_post
from newsletter_archive.search.judge import Judgment, load_judgments, save_judgments

from .cli_helpers import CliEnv
from .conftest import make_config

SLUGS = ["alpha", "beta"]
EVALS = """# Search evaluation questions (filler).
# Post ids = file names in posts/
questions:
  - id: q1
    type: local
    question: how do teams plan their week
    expected: [alpha, beta]  # two posts
  - id: q2
    type: intent
    question: what helps a new lead
    expected: ['beta']
"""


def v1_record(slug: str) -> dict:
    text = f"Filler text for {slug}. " * 20
    return {
        "schema_version": 1,
        "id": slug,
        "url": f"https://www.example.com/{slug}/",
        "title": f"Post {slug}",
        "subtitle": None,
        "author": "Author",
        "published_date": "2025-01-02",
        "categories": ["Leadership"],
        "word_count": len(text.split()),
        "summary": "Short description.",
        "sections": [{"heading": None, "level": 0, "text": text}],
        "text": text,
        "links": [],
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        "fetched_at": "2026-10-31T00:00:00Z",
        "status": "ok",
    }


def old_archive(out: Path, slugs=SLUGS, evals: str | None = EVALS, judged: bool = True) -> None:
    """Write a single-newsletter archive the way the old layout kept it."""
    (out / "raw").mkdir(parents=True)
    (out / "posts").mkdir()
    manifest = {"posts": [{"url": f"https://www.example.com/{s}", "slug": s, "status": "ok"} for s in slugs]}
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for s in slugs:
        (out / "raw" / f"{s}.html").write_text(f"<html>{s}</html>", encoding="utf-8")
        (out / "posts" / f"{s}.json").write_text(json.dumps(v1_record(s)), encoding="utf-8")
    if evals is not None:
        (out / "eval-queries.yaml").write_text(evals, encoding="utf-8")
    if judged:
        save_judgments(out / "eval-judgments.yaml", {"q1": Judgment("q", "m", 1, {"alpha": 3, "beta": 1})})
    (out / "logs").mkdir()
    (out / "logs" / "run.log").write_text("log", encoding="utf-8")
    (out / "postgres").mkdir()
    (out / "postgres" / "PG_VERSION").write_text("17", encoding="utf-8")


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path)


def snapshot(out: Path) -> dict[str, bytes]:
    return {str(p.relative_to(out)): p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}


# spec: newsletter-sources / Move the existing data once (Move the current archive)
def test_full_move(cfg):
    out = cfg.output_dir
    old_archive(out)
    shared_before = {k: v for k, v in snapshot(out).items() if k.startswith(("logs", "postgres"))}
    result = migrate(cfg, "main")
    main = cfg.source("main")

    assert (result.raw, result.posts, result.eval_ids) == (2, 2, 5)
    assert main.manifest_path.exists() and not (out / "manifest.json").exists()
    assert not (out / "raw").exists() and not (out / "posts").exists()
    assert sorted(p.name for p in main.raw_dir.iterdir()) == ["alpha.html", "beta.html"]
    assert main.raw_path("alpha").read_text() == "<html>alpha</html>"
    for slug in SLUGS:
        rec = json.loads(main.post_path(slug).read_text())
        validate_post(rec)
        assert (rec["schema_version"], rec["id"], rec["source"]) == (2, f"main/{slug}", "main")
    evals = (out / "eval-queries.yaml").read_text()
    assert "expected: [main/alpha, main/beta]  # two posts" in evals
    assert "expected: ['main/beta']" in evals
    assert evals.startswith("# Search evaluation questions (filler).\n# Post ids = file names in posts/\n")
    assert load_judgments(out / "eval-judgments.yaml")["q1"].grades == {"main/alpha": 3, "main/beta": 1}
    assert {k: v for k, v in snapshot(out).items() if k.startswith(("logs", "postgres"))} == shared_before

    lines = result.lines(main, dry_run=False)
    assert lines[0].startswith("Moved into main/: the manifest, 2 saved pages and 2 posts; 5 post ids")
    assert lines[-1].startswith("Next: start the database")


# spec: newsletter-sources / Move the existing data once (Dry run)
def test_dry_run_changes_nothing(cfg):
    out = cfg.output_dir
    old_archive(out)
    before = snapshot(out)
    result = migrate(cfg, "main", dry_run=True)
    assert snapshot(out) == before and not cfg.source("main").root.exists()
    assert (result.raw, result.posts, result.eval_ids) == (2, 2, 5)
    assert result.examples["pages"] == [
        "raw/alpha.html -> main/raw/alpha.html",
        "raw/beta.html -> main/raw/beta.html",
    ]
    lines = result.lines(cfg.source("main"), dry_run=True)
    assert lines[0].startswith("Would move into main/") and "would be renamed" in lines[0]
    assert "  posts: posts/alpha.json -> main/posts/alpha.json" in lines
    assert not any(line.startswith("Next:") for line in lines)


def test_second_run_has_nothing_to_move(cfg):
    old_archive(cfg.output_dir)
    migrate(cfg, "main")
    after = snapshot(cfg.output_dir)
    result = migrate(cfg, "main")
    assert result.nothing_to_move and snapshot(cfg.output_dir) == after
    assert result.lines(cfg.source("main"), dry_run=False) == [
        "Nothing to move: no single-newsletter archive found (already moved?)."
    ]


def test_without_eval_files(cfg):
    old_archive(cfg.output_dir, evals=None, judged=False)
    result = migrate(cfg, "main")
    assert result.eval_ids == 0 and not (cfg.output_dir / "eval-queries.yaml").exists()


def test_other_files_keep_the_old_folders(cfg):
    out = cfg.output_dir
    old_archive(out)
    (out / "raw" / ".DS_Store").write_text("x")
    migrate(cfg, "main")
    assert (out / "raw" / ".DS_Store").exists() and not (out / "posts").exists()


# spec: newsletter-sources / Move the existing data once (Interrupted move)
def test_crash_in_the_commit_phase_then_rerun(cfg, monkeypatch):
    out = cfg.output_dir
    old_archive(out)
    real_unlink = Path.unlink
    calls = {"n": 0}

    def flaky_unlink(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk went away")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    with pytest.raises(OSError):
        migrate(cfg, "main")
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert (out / "manifest.json").exists() and (out / "posts" / "alpha.json").exists()

    migrate(cfg, "main")
    assert not (out / "manifest.json").exists() and not (out / "raw").exists()
    assert json.loads(cfg.source("main").post_path("beta").read_text())["id"] == "main/beta"
    assert load_judgments(out / "eval-judgments.yaml")["q1"].grades == {"main/alpha": 3, "main/beta": 1}


def test_crash_in_the_write_phase_keeps_every_old_file(cfg, monkeypatch):
    out = cfg.output_dir
    old_archive(out)
    before = {k: v for k, v in snapshot(out).items() if k.startswith(("raw", "posts", "manifest"))}
    real = migrate_module.write_json_atomic
    calls = {"n": 0}

    def flaky(path, obj):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("killed")
        real(path, obj)

    monkeypatch.setattr(migrate_module, "write_json_atomic", flaky)
    with pytest.raises(OSError):
        migrate(cfg, "main")
    assert {k: v for k, v in snapshot(out).items() if k.startswith(("raw", "posts", "manifest"))} == before
    monkeypatch.setattr(migrate_module, "write_json_atomic", real)
    migrate(cfg, "main")
    assert not (out / "manifest.json").exists() and cfg.source("main").post_path("beta").exists()


def test_missing_new_files_stop_before_deleting(cfg, monkeypatch):
    out = cfg.output_dir
    old_archive(out)
    monkeypatch.setattr(migrate_module, "write_json_atomic", lambda path, obj: None)
    with pytest.raises(ArchiveError, match="not deleting anything: 2 new files are missing"):
        migrate(cfg, "main")
    assert (out / "manifest.json").exists() and (out / "posts" / "alpha.json").exists()


def test_a_different_manifest_in_the_source_folder_is_refused(cfg):
    out = cfg.output_dir
    old_archive(out)
    main = cfg.source("main")
    main.root.mkdir()
    main.manifest_path.write_text('{"posts": []}')
    before = snapshot(out)
    with pytest.raises(ArchiveError, match="already exists and differs"):
        migrate(cfg, "main")
    assert snapshot(out) == before


def test_unknown_source(cfg):
    old_archive(cfg.output_dir)
    with pytest.raises(ConfigError, match="unknown source 'nope'"):
        migrate(cfg, "nope")


def test_bad_post_file_stops_before_deleting(cfg):
    out = cfg.output_dir
    old_archive(out)
    bad = v1_record("beta") | {"schema_version": 7}
    (out / "posts" / "beta.json").write_text(json.dumps(bad))
    with pytest.raises(ArchiveError, match="unknown schema_version"):
        migrate(cfg, "main")
    assert (out / "manifest.json").exists() and (out / "posts" / "alpha.json").exists()


# -- prefix_eval_queries ------------------------------------------------------


def test_inline_and_quoted_lists():
    text, changed = prefix_eval_queries(EVALS, "main")
    assert changed == 3
    assert "expected: [main/alpha, main/beta]  # two posts" in text and "expected: ['main/beta']" in text


def test_block_lists_and_ids_already_prefixed():
    text = (
        "questions:\n"
        "  - id: q1\n"
        "    expected:  # a block list\n"
        "      - alpha\n"
        '      - "beta"  # quoted\n'
        "      - main/gamma\n"
        "  - id: q2\n"
        "    expected: [main/alpha, delta]\n"
    )
    new, changed = prefix_eval_queries(text, "main")
    assert changed == 3
    assert "      - main/alpha\n" in new and '      - "main/beta"  # quoted\n' in new
    assert "      - main/gamma\n" in new and "  - id: q2\n" in new
    assert "expected: [main/alpha, main/delta]" in new


def test_already_prefixed_text_is_unchanged():
    once, _ = prefix_eval_queries(EVALS, "main")
    twice, changed = prefix_eval_queries(once, "main")
    assert twice == once and changed == 0


def test_a_form_the_rewrite_cannot_handle_is_refused():
    text = "questions:\n  - id: q1\n    expected: [alpha,\n      beta]\n"
    with pytest.raises(ArchiveError, match=r"could not rewrite eval-queries\.yaml safely"):
        prefix_eval_queries(text, "main")


def test_invalid_yaml_is_refused():
    with pytest.raises(ArchiveError, match="not valid YAML"):
        prefix_eval_queries("questions: [\n", "main")


def test_text_without_questions_is_left_alone():
    assert prefix_eval_queries("# nothing here\n", "main") == ("# nothing here\n", 0)


# -- CLI ------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    return CliEnv(tmp_path, monkeypatch)


def test_cli_dry_run_then_move(env):
    old_archive(env.data)
    r = env.run("migrate", "--source", "main", "--dry-run")
    assert r.exit_code == 0, r.output
    assert "Would move into main/" in r.output and (env.data / "manifest.json").exists()
    r = env.run("migrate", "--source", "main")
    assert r.exit_code == 0, r.output
    assert "Moved into main/" in r.output and (env.data / "main" / "manifest.json").exists()


def test_cli_reports_errors(env):
    old_archive(env.data)
    r = env.run("migrate", "--source", "nope")
    assert r.exit_code == 1 and "unknown source 'nope'" in r.output


def test_cli_migrate_error_from_the_move(env):
    old_archive(env.data)
    (env.data / "main").mkdir()
    (env.data / "main" / "manifest.json").write_text('{"posts": []}')
    r = env.run("migrate", "--source", "main")
    assert r.exit_code == 1 and "already exists and differs" in r.output


# spec: newsletter-sources / Move the existing data once (Old layout detected)
def test_other_commands_refuse_the_old_layout(env):
    old_archive(env.data)
    r = env.run("extract")
    assert r.exit_code == 1 and "archive migrate" in r.output
    assert (env.data / "posts" / "alpha.json").exists()


@pytest.mark.parametrize(
    "content, message",
    [("not json {", "is not valid JSON"), ("[1, 2]", "is not a post record")],
)
def test_corrupt_post_file_stops_before_deleting(cfg, content, message):
    out = cfg.output_dir
    old_archive(out)
    (out / "posts" / "beta.json").write_text(content)
    with pytest.raises(ArchiveError, match=f"posts/beta.json {message}"):
        migrate(cfg, "main")
    assert (out / "manifest.json").exists() and (out / "posts" / "alpha.json").exists()


def test_cli_reports_file_errors_without_a_traceback(env, monkeypatch):
    import newsletter_archive.migrate as migrate_module

    old_archive(env.data)

    def disk_full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(migrate_module, "migrate", disk_full)
    r = env.run("migrate", "--source", "main")
    assert r.exit_code == 1 and "Error:" in r.output and "No space left" in r.output
    assert "Traceback" not in r.output

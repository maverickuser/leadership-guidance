import json

import pytest

from newsletter_archive.errors import MISSING_COOKIE_MESSAGE, NOT_SIGNED_IN_MESSAGE

from .cli_helpers import BASE, CliEnv
from .sitebuilder import ghost_site

SLUGS = ["p1", "p2", "p3", "p4", "p5", "p6", "p7"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = CliEnv(tmp_path, monkeypatch)
    ghost_site(e.transport, BASE, SLUGS)
    return e


# spec: archive-cli / Run all stages; Limit; End of run
def test_run_limit_end_to_end(env):
    r = env.run("run", "--limit", "5")
    assert r.exit_code == 0, r.output
    assert len(list((env.data / "raw").iterdir())) == 5
    assert len(list((env.data / "posts").iterdir())) == 5
    assert len(list((env.data / "logs").iterdir())) == 1
    assert "Summary: discovered=7 fetched=5 ok=5 partial=0 failed=0" in r.output
    urls = env.transport.urls()
    assert urls.index(f"{BASE}/sitemap.xml") < urls.index(f"{BASE}/p1/")  # discover before fetch
    post = json.loads((env.data / "posts" / "p1.json").read_text())
    assert post["categories"] == ["Career Growth"]


def test_second_run_downloads_nothing(env):
    env.run("run", "--limit", "5")
    before = [u for u in env.transport.urls() if u.endswith(tuple(f"/{s}/" for s in SLUGS))]
    r = env.run("run", "--limit", "5")
    after = [u for u in env.transport.urls() if u.endswith(tuple(f"/{s}/" for s in SLUGS))]
    assert r.exit_code == 0 and before == after
    assert "fetched=0" in r.output and "skipped=5" in r.output and "unchanged=5" in r.output


def test_one_client_shared_and_spacing_kept_across_stages(env):
    env.run("run", "--limit", "2")
    # every request after the first waited out the 2s gap (fake clock, no real waiting)
    gaps = [b.at - a.at for a, b in zip(env.transport.calls, env.transport.calls[1:], strict=False)]
    hops_in_same_slot = sum(1 for g in gaps if g == 0)  # /t/ -> /tag/ redirect hops
    assert hops_in_same_slot == 1
    assert all(g >= 2 for g in gaps if g != 0)


def test_stage_commands(env):
    assert env.run("discover").exit_code == 0
    assert json.loads((env.data / "manifest.json").read_text())["posts"][0]["status"] == "discovered"
    assert env.run("fetch", "--limit", "2").exit_code == 0
    assert sorted(p.name for p in (env.data / "raw").iterdir()) == ["p1.html", "p2.html"]
    r = env.run("extract")
    assert r.exit_code == 0 and "ok=2" in r.output


def test_discover_limit_caps_new_posts(env):
    env.run("discover", "--limit", "3")
    assert len(json.loads((env.data / "manifest.json").read_text())["posts"]) == 3


# spec: archive-cli / Dry run
def test_fetch_dry_run_writes_nothing(env):
    env.run("discover")
    calls = len(env.transport.calls)
    r = env.run("fetch", "--dry-run")
    assert r.exit_code == 0
    assert "p1  fetch  would-fetch" in r.output
    assert len(env.transport.calls) == calls
    assert not (env.data / "raw").exists() and not (env.data / "posts").exists()


def test_run_dry_run_lists_new_posts_without_writing(env):
    r = env.run("run", "--dry-run", "--limit", "2")
    assert r.exit_code == 0
    assert "p1  fetch  would-fetch" in r.output and "p2  fetch  would-fetch" in r.output
    assert not (env.data / "manifest.json").exists()
    assert not any(u.endswith("/p1/") for u in env.transport.urls())


# spec: post-fetching / Cookie missing (exit 1, no requests)
def test_missing_cookie_exit_1(tmp_path, monkeypatch):
    e = CliEnv(tmp_path, monkeypatch, cookie="")
    ghost_site(e.transport, BASE, SLUGS)
    r = e.run("run", "--limit", "1")
    assert r.exit_code == 1 and MISSING_COOKIE_MESSAGE in r.output
    assert e.transport.calls == []


def test_discover_works_without_cookie(tmp_path, monkeypatch):
    e = CliEnv(tmp_path, monkeypatch, cookie="")
    ghost_site(e.transport, BASE, SLUGS)
    assert e.run("discover").exit_code == 0


# spec: post-fetching / Expired cookie on first post (exit 2)
def test_signed_out_exit_2(tmp_path, monkeypatch):
    e = CliEnv(tmp_path, monkeypatch)
    ghost_site(e.transport, BASE, SLUGS, signed_out={"p1"})
    r = e.run("run", "--limit", "3")
    assert r.exit_code == 2 and NOT_SIGNED_IN_MESSAGE in r.output
    assert "Summary:" in r.output
    assert not (e.data / "raw" / "p1.html").exists()


def test_bad_config_exit_1(env):
    env.config.write_text(f"base_url: {BASE}\ndelay_seconds: 0\n")
    r = env.run("discover")
    assert r.exit_code == 1 and "delay_seconds" in r.output


def test_keyboard_interrupt_exit_130(env, monkeypatch):
    import newsletter_archive.cli as cli

    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_discover", boom)
    r = env.run("discover")
    assert r.exit_code == 130 and "Progress so far is saved" in r.output


def test_unexpected_error_exit_1_and_logged(env, monkeypatch):
    import newsletter_archive.cli as cli

    monkeypatch.setattr(cli, "run_discover", lambda *a, **k: 1 / 0)
    r = env.run("discover")
    assert r.exit_code == 1 and "Unexpected error" in r.output
    log_text = next((env.data / "logs").iterdir()).read_text()
    assert "ZeroDivisionError" in log_text


def test_per_post_lines_in_log_file(env):
    env.run("run", "--limit", "2")
    log_text = next((env.data / "logs").iterdir()).read_text()
    assert "p1  fetch  fetched" in log_text and "p1  extract  ok" in log_text


def test_version():
    from typer.testing import CliRunner

    import newsletter_archive.cli as cli

    r = CliRunner().invoke(cli.app, ["--version"])
    assert r.exit_code == 0 and r.output.strip() == "0.1.0"


# --- index stage (add-hybrid-search) -----------------------------------------------------------


def test_run_ends_with_index(env):
    r = env.run("run", "--limit", "2")
    assert r.exit_code == 0, r.output
    assert len(env.index_calls) == 1 and env.index_calls[0].limit == 2
    assert "Index status: ok" in r.output


def test_index_command_passes_options(env):
    r = env.run("index", "--dry-run", "--retry-failed", "--allow-mass-delete", "--yes", "--limit", "3")
    assert r.exit_code == 0, r.output
    o = env.index_calls[0]
    assert (o.dry_run, o.retry_failed, o.allow_mass_delete, o.yes, o.limit) == (True, True, True, True, 3)


def test_index_errors_exit_1_alone_and_3_in_run(env):
    from newsletter_archive.search.gemini import MissingGeminiKeyError

    env.index_result = MissingGeminiKeyError()
    r = env.run("index")
    assert r.exit_code == 1 and "GEMINI_API_KEY is not set" in r.output
    r = env.run("run", "--limit", "1")
    assert r.exit_code == 3 and "Index step failed" in r.output


def test_stuck_posts_exit_3_in_run(env):
    from newsletter_archive.search.indexer import IndexReport

    env.index_result = IndexReport(stuck=["p1"], status="partial")
    assert env.run("run", "--limit", "1").exit_code == 3
    assert env.run("index").exit_code == 0  # the index command itself only reports it


def test_unexpected_index_errors_exit_3_in_run(env):
    import psycopg

    env.index_result = psycopg.OperationalError("server closed the connection")
    r = env.run("run", "--limit", "1")
    assert r.exit_code == 3 and "search.enabled: false" in r.output
    env.index_result = RuntimeError("bug")
    assert env.run("run", "--limit", "1").exit_code == 3
    assert env.run("index").exit_code == 1  # alone, an unexpected error is a plain failure


def test_outage_exits_3_in_run(env):
    from newsletter_archive.search.indexer import IndexReport

    env.index_result = IndexReport(breaker_open=True, status="partial")
    assert env.run("run", "--limit", "1").exit_code == 3


def test_search_can_be_turned_off_for_run(env):
    env.config.write_text(env.config.read_text() + "search:\n  enabled: false\n", encoding="utf-8")
    r = env.run("run", "--limit", "1")
    assert r.exit_code == 0 and env.index_calls == [] and "Search is turned off" in r.output

"""scripts/scheduled-run.sh: the notification for each way `archive run` can end.

The script is run with a fake `.venv/bin/archive` that prints a canned run and exits with the
code under test, and a fake `osascript` that records the notification text."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scheduled-run.sh"
TITLE = ' with title "Newsletter archive"'

TWO_SOURCES = "== main: discover\n== main: fetch\n== The Pragmatic Engineer: discover\n== index\n"
PE_STOPPED = (
    "Stopped: The Pragmatic Engineer (PRAGMATIC_ENGINEER_COOKIE): "
    "your cookie looks expired. Paste a fresh Cookie header into .env\n"
)
MAIN_STOPPED = (
    "Stopped: main (NEWSLETTER_COOKIE): your cookie looks expired. Paste a fresh Cookie header into .env\n"
)
PE_NOTICE = (
    "The Pragmatic Engineer: your cookie looks expired. Paste a fresh Cookie header into .env "
    "(PRAGMATIC_ENGINEER_COOKIE)."
)
MAIN_NOTICE = "main: your cookie looks expired. Paste a fresh Cookie header into .env (NEWSLETTER_COOKIE)."


def executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def repo(tmp_path):
    """A folder shaped like the repo: scripts/scheduled-run.sh and a fake .venv/bin/archive."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / "scheduled-run.sh")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    executable(
        tmp_path / ".venv" / "bin" / "archive",
        '#!/bin/sh\n[ "$1" = run ] || exit 99\ncat "$FAKE_RUN_OUTPUT"\nexit "$FAKE_RUN_CODE"\n',
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    executable(fake_bin / "osascript", '#!/bin/sh\nprintf "%s\\n" "$2" >> "$FAKE_NOTIFICATIONS"\n')
    return tmp_path


def run_script(repo: Path, code: int, output: str) -> tuple[int, list[str]]:
    """Run the script; return its exit code and the notification texts it sent."""
    (repo / "run-output.txt").write_text(output, encoding="utf-8")
    notifications = repo / "notifications.txt"
    notifications.unlink(missing_ok=True)
    env = dict(
        os.environ,
        PATH=f"{repo / 'fake-bin'}:{os.environ.get('PATH', '')}",
        FAKE_RUN_OUTPUT=str(repo / "run-output.txt"),
        FAKE_RUN_CODE=str(code),
        FAKE_NOTIFICATIONS=str(notifications),
        ARCHIVE_SKIP_DB_START="1",  # never start the real database from a test
    )
    done = subprocess.run(
        ["bash", str(repo / "scripts" / "scheduled-run.sh")], env=env, capture_output=True, text=True
    )
    sent = notifications.read_text(encoding="utf-8").splitlines() if notifications.exists() else []
    texts = []
    for line in sent:
        m = re.fullmatch(r'display notification "(.*)"' + re.escape(TITLE), line)
        assert m, line
        texts.append(m.group(1))
    return done.returncode, texts


# spec: newsletter-sources / Weekly run covers every source (Both sources updated)
def test_new_posts_across_newsletters_are_counted_together(repo):
    code, texts = run_script(
        repo, 0, TWO_SOURCES + "Summary: discovered=11 fetched=2 ok=2 partial=0 failed=0\n"
    )
    assert (code, texts) == (0, ["Archived 2 new post(s)."])


def test_nothing_new_sends_no_notification(repo):
    code, texts = run_script(
        repo, 0, TWO_SOURCES + "Summary: discovered=11 fetched=0 ok=0 partial=0 failed=0\n"
    )
    assert (code, texts) == (0, [])


# spec: newsletter-sources / Weekly run covers every source (One login expired in the weekly run)
def test_one_expired_login_names_the_newsletter(repo):
    output = TWO_SOURCES + "Summary: discovered=11 fetched=1 ok=1 partial=0 failed=0\n" + PE_STOPPED
    code, texts = run_script(repo, 2, output)
    assert (code, texts) == (2, [PE_NOTICE + " Others updated."])


def test_two_expired_logins_list_both(repo):
    output = (
        TWO_SOURCES + "Summary: discovered=11 fetched=0 ok=0 partial=0 failed=0\n" + MAIN_STOPPED + PE_STOPPED
    )
    code, texts = run_script(repo, 2, output)
    assert (code, texts) == (2, [f"{MAIN_NOTICE} {PE_NOTICE}"])


def test_missing_cookie_for_one_newsletter_is_named_too(repo):
    output = (
        "== main: discover\n== index\nSummary: discovered=8 fetched=0 ok=0 partial=0 failed=0\n"
        "Stopped: The Pragmatic Engineer (PRAGMATIC_ENGINEER_COOKIE): cookie is not set. "
        "Copy your Cookie header into .env\n"
    )
    code, texts = run_script(repo, 1, output)
    assert code == 1
    assert texts == [
        "The Pragmatic Engineer: cookie is not set. Copy your Cookie header into .env "
        "(PRAGMATIC_ENGINEER_COOKIE). Others updated."
    ]


def test_exit_2_without_a_stopped_line_keeps_the_old_text(repo):
    code, texts = run_script(repo, 2, "Summary: discovered=8 fetched=0 ok=0 partial=0 failed=0\n")
    assert (code, texts) == (2, ["Your cookie looks expired. Paste a fresh Cookie header into .env."])


def test_index_failure_and_other_failures(repo):
    code, texts = run_script(repo, 3, "Index step failed: database down\nSummary: fetched=1\n")
    assert code == 3 and texts[0].startswith("New posts were archived, but updating the search index failed")
    code, texts = run_script(repo, 1, "Error: bad config\n")
    assert (code, texts) == (1, ["Update failed (exit 1). See the latest log in the archive's logs folder."])

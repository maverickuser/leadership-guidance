"""scripts/check_safety.py on a throwaway git repo: API keys and database passwords."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_safety.py"
FAKE_GOOGLE_KEY = "AIza" + "Sy" + "x" * 33  # built at runtime so this file never holds one
PW_VAR = "ARCHIVE_DB_{}_" + "PASSWORD"  # split so the safety check does not flag this file


def run_check(tmp_path: Path, files: dict[str, str]) -> subprocess.CompletedProcess[str]:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return subprocess.run([sys.executable, str(SCRIPT)], cwd=tmp_path, capture_output=True, text=True)


@pytest.mark.parametrize(
    "files, problem",
    [
        ({"notes.md": f"key = {FAKE_GOOGLE_KEY}\n"}, "looks like a Google API key"),
        ({"compose.yml": PW_VAR.format("READER") + "=hunter2hunter2\n"}, "database password"),
        ({"config.yml": PW_VAR.format("WRITER") + ": 'Sup3rS3cret!'\n"}, "database password"),
        ({".env": "GEMINI_API_KEY=x\n"}, "private file is tracked"),
    ],
)
def test_secrets_fail_the_check(tmp_path, files, problem):
    result = run_check(tmp_path, files)
    assert result.returncode == 1 and problem in result.stdout


def test_placeholders_and_references_pass(tmp_path):
    result = run_check(
        tmp_path,
        {
            ".env.example": "GEMINI_API_KEY=\nARCHIVE_DB_READER_PASSWORD=<choose one>\n",
            "compose.yml": "ARCHIVE_DB_READER_PASSWORD: ${ARCHIVE_DB_READER_PASSWORD:?set it}\n",
            "ci.yml": "ARCHIVE_DB_WRITER_PASSWORD=ci-writer\nARCHIVE_DB_ADMIN_PASSWORD=test-admin\n",
        },
    )
    assert result.returncode == 0, result.stdout


SUBSTACK_SID = "connect" + ".sid"  # split so the safety check does not flag this file


@pytest.mark.parametrize(
    "files, problem",
    [
        ({"notes.md": f"{SUBSTACK_SID}=s%3AaB3dEf9GhIjK.realLookingValue\n"}, "Substack session cookie"),
        ({"notes.md": "substack" + ".sid=s%3AaB3dEf9GhIjKlMnOp\n"}, "Substack session cookie"),
        ({"setup.md": "PRAGMATIC_ENGINEER" + "_COOKIE=abc123realvalue\n"}, "PRAGMATIC_ENGINEER_COOKIE has"),
    ],
)
def test_substack_and_named_cookie_values_fail(tmp_path, files, problem):
    result = run_check(tmp_path, files)
    assert result.returncode == 1 and problem in result.stdout


def test_substack_fakes_and_placeholders_pass(tmp_path):
    result = run_check(
        tmp_path,
        {
            "tests/a.py": f'COOKIE = "{SUBSTACK_SID}=TESTSECRET123"\n',
            ".env.example": "# PRAGMATIC_ENGINEER" + "_COOKIE=\nPE" + "_COOKIE=<paste the Cookie header>\n",
            "README.md": "`PRAGMATIC_ENGINEER" + "_COOKIE=` in .env\nX" + "_COOKIE=${SECRET}\n",
        },
    )
    assert result.returncode == 0, result.stdout


SUBSTACK_LLI = "substack" + ".lli"


@pytest.mark.parametrize(
    "text",
    [
        f'{{"{SUBSTACK_SID}": "s%3AaB3dEf9GhIjKlMnOp"}}\n',  # JSON export
        f"newsletter.example.org\tFALSE\t/\tTRUE\t0\t{SUBSTACK_SID}\ts%3AaB3dEf9GhIjKlMnOp\n",  # cookies.txt
        f'{SUBSTACK_LLI}="s%3AaB3dEf9GhIjKlMnOp"\n',  # quoted
    ],
)
def test_substack_cookie_in_any_export_format_fails(tmp_path, text):
    result = run_check(tmp_path, {"notes.txt": text})
    assert result.returncode == 1 and "Substack session cookie" in result.stdout


@pytest.mark.parametrize(
    "line",
    [
        "NEWSLETTER" + '_COOKIE="s%3AaB3dEf9GhIjKlMnOp"\n',  # quoted
        "NEWSLETTER" + "_COOKIE='realvaluerealvalue'\n",
        "X" + "_COOKIE=" + "substack" + ".sidREALVALUEREAL\n",  # an allowed-looking prefix
    ],
)
def test_quoted_or_disguised_env_cookies_fail(tmp_path, line):
    result = run_check(tmp_path, {"setup.md": line})
    assert result.returncode == 1 and "_COOKIE has" in result.stdout


def test_short_test_values_pass(tmp_path):
    result = run_check(tmp_path, {"tests/b.py": "X" + '_COOKIE="a=1\\nX: 2"\n'})
    assert result.returncode == 0, result.stdout

"""scripts/check_safety.py on a throwaway git repo: API keys and database passwords."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_safety.py"
FAKE_GOOGLE_KEY = "AIza" + "Sy" + "x" * 33  # built at runtime so this file never holds one


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
        ({"compose.yml": "ARCHIVE_DB_READER_PASSWORD=hunter2hunter2\n"}, "database password"),
        ({"config.yml": "ARCHIVE_DB_WRITER_PASSWORD: 'Sup3rS3cret!'\n"}, "database password"),
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

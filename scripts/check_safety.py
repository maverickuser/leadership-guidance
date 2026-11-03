#!/usr/bin/env python3
"""Fail if tracked files could leak private data in this public repo.

Checks: no tracked data/ or .env* files (except .env.example), no real cookie values
in the cookie env variable or in Ghost member cookies, no Google API keys, no literal
database passwords, and no emails or uuids in test fixtures. Run in CI and before pushing.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ALLOWED_FAKE_VALUES = {"TESTSECRET123", "SIGSECRET456"}
COOKIE_RE = re.compile(r"ghost-members-ssr(?:\.sig)?=([A-Za-z0-9%._\-]+)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
FIXTURE_EMAIL_ALLOW = {"noreply@example.com"}
# NEWSLETTER_COOKIE=<literal>; placeholders, f-string fields and the test fakes are fine
ENV_COOKIE_RE = re.compile(r"NEWSLETTER_COOKIE=([^\s'\"]+)")
ENV_COOKIE_OK = re.compile(r"^(\{|<|ghost-members-ssr|from-file\b)")
GOOGLE_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{35}")
# ARCHIVE_DB_*_PASSWORD=<literal> or `: <literal>`; references, placeholders and ci-/test- values are fine
DB_PASSWORD_RE = re.compile(r"ARCHIVE_DB_[A-Z]+_PASSWORD\s*[:=][ \t]*[\"']?([^\s'\"]+)")
DB_PASSWORD_OK = re.compile(r"^(\$|\?|\{|<|ci-|test-)")


def is_private_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    is_env = name == ".env" or (name.startswith(".env.") and name != ".env.example")
    return is_env or path.startswith("data/")


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout
    return [p for p in out.decode().split("\0") if p]


def main() -> int:
    problems: list[str] = []
    for path in tracked_files():
        if path == "scripts/check_safety.py":  # its own patterns would match
            continue
        if is_private_path(path):
            problems.append(f"{path}: private file is tracked")
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for m in COOKIE_RE.finditer(text):
            if m.group(1) not in ALLOWED_FAKE_VALUES:
                problems.append(f"{path}: looks like a real ghost-members-ssr cookie value")
        for m in ENV_COOKIE_RE.finditer(text):
            if not ENV_COOKIE_OK.match(m.group(1)):
                problems.append(f"{path}: NEWSLETTER_COOKIE has what looks like a real value")
        if GOOGLE_KEY_RE.search(text):
            problems.append(f"{path}: looks like a Google API key")
        for m in DB_PASSWORD_RE.finditer(text):
            if not DB_PASSWORD_OK.match(m.group(1)):
                problems.append(f"{path}: database password has what looks like a real value")
        if path.startswith("tests/fixtures/"):
            for m in EMAIL_RE.finditer(text):
                if m.group(0).lower() not in FIXTURE_EMAIL_ALLOW:
                    problems.append(f"{path}: email address in fixture")
                    break
            if UUID_RE.search(text):
                problems.append(f"{path}: uuid in fixture")
    for p in problems:
        print(f"SAFETY: {p}")
    print("safety check:", "FAILED" if problems else "ok")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

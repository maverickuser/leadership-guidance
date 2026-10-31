#!/usr/bin/env python3
"""Fail if tracked files could leak private data in this public repo.

Checks: no tracked data/ or .env files, no real ghost-members-ssr cookie values,
and no emails or uuids in test fixtures. Run in CI and before pushing.
"""

from __future__ import annotations

import re
import subprocess
import sys

ALLOWED_FAKE_VALUES = {"TESTSECRET123", "SIGSECRET456"}
COOKIE_RE = re.compile(r"ghost-members-ssr(?:\.sig)?=([A-Za-z0-9%._\-]+)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
FIXTURE_EMAIL_ALLOW = {"noreply@example.com"}


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout
    return [p for p in out.decode().split("\0") if p]


def main() -> int:
    problems: list[str] = []
    for path in tracked_files():
        if path == ".env" or path.startswith("data/") or path.endswith("/.env"):
            problems.append(f"{path}: private file is tracked")
            continue
        try:
            text = open(path, encoding="utf-8").read()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for m in COOKIE_RE.finditer(text):
            if m.group(1) not in ALLOWED_FAKE_VALUES:
                problems.append(f"{path}: looks like a real ghost-members-ssr cookie value")
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

"""Shared test setup: block non-loopback network, build configs in a temp dir."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from newsletter_archive.config import Config, Secret

FIXTURES = Path(__file__).parent / "fixtures"

# Fake cookie values. They are allowlisted by scripts/check_safety.py.
TEST_COOKIE = "ghost-members-ssr=TESTSECRET123; ghost-members-ssr.sig=SIGSECRET456"
SECRET_PARTS = ("TESTSECRET123", "SIGSECRET456")
# Fake search secrets, shaped like the real ones. Allowlisted by scripts/check_safety.py.
TEST_GEMINI_KEY = "AIza" + "TESTKEY" + "0" * 28
TEST_DB_PASSWORD = "test-db-password-123"

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_getaddrinfo = socket.getaddrinfo


def _guarded_connect(self, address):  # pragma: no cover - exercised implicitly
    host = address[0] if isinstance(address, tuple) else address
    if self.family == getattr(socket, "AF_UNIX", None) or host in _LOOPBACK:
        return _real_connect(self, address)
    raise RuntimeError(f"network access blocked in tests: {address!r}")


def _guarded_connect_ex(self, address):  # pragma: no cover - exercised implicitly
    host = address[0] if isinstance(address, tuple) else address
    if self.family == getattr(socket, "AF_UNIX", None) or host in _LOOPBACK:
        return _real_connect_ex(self, address)
    raise RuntimeError(f"network access blocked in tests: {address!r}")


def _guarded_getaddrinfo(host, *args, **kwargs):  # pragma: no cover - exercised implicitly
    if host in _LOOPBACK or host is None:
        return _real_getaddrinfo(host, *args, **kwargs)
    raise RuntimeError(f"DNS lookup blocked in tests: {host!r}")


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    for name in (
        "NEWSLETTER_COOKIE",
        "GEMINI_API_KEY",
        "ARCHIVE_DB_WRITER_PASSWORD",
        "ARCHIVE_DB_READER_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)


def make_config(tmp_path: Path, **overrides) -> Config:
    values = dict(
        base_url="https://www.example.com",
        delay_seconds=2.0,
        max_retries=3,
        timeout_seconds=30.0,
        min_word_count=300,
        output_dir=tmp_path / "data",
        user_agent="test-agent/1.0",
        cookie=Secret(TEST_COOKIE),
    )
    values.update(overrides)
    return Config(**values)


@pytest.fixture
def cfg(tmp_path) -> Config:
    return make_config(tmp_path)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")

"""Run the CLI in-process against a fake site."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import newsletter_archive.cli as cli
from newsletter_archive.http import Client
from newsletter_archive.search.indexer import IndexReport

from .conftest import TEST_COOKIE
from .fakes import FakeClock, FakeTransport

BASE = "https://www.example.com"


class CliEnv:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cookie: str = TEST_COOKIE):
        self.root = tmp_path
        self.data = tmp_path / "data"
        self.config = tmp_path / "config.yaml"
        self.config.write_text(f"base_url: {BASE}\noutput_dir: data\n", encoding="utf-8")
        (tmp_path / ".env").write_text(f"NEWSLETTER_COOKIE={cookie}\n", encoding="utf-8")
        self.clock = FakeClock()
        self.transport = FakeTransport(clock=self.clock)
        monkeypatch.setattr(
            cli,
            "make_client",
            lambda cfg: Client(cfg, transport=self.transport, sleep=self.clock.sleep, clock=self.clock.clock),
        )
        # The index stage is stubbed here (it needs the database); see tests/search for it.
        self.index_calls: list = []
        self.index_result: object = IndexReport()

        def fake_index(cfg, options, on_post):
            self.index_calls.append(options)
            if isinstance(self.index_result, BaseException):
                raise self.index_result
            return self.index_result

        monkeypatch.setattr(cli, "index_archive", fake_index)
        self.runner = CliRunner()

    def run(self, *args: str):
        return self.runner.invoke(cli.app, [*args, "--config", str(self.config)])

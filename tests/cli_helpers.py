"""Run the CLI in-process against a fake site."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import newsletter_archive.cli as cli
from newsletter_archive.http import Client

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
        self.runner = CliRunner()

    def run(self, *args: str):
        return self.runner.invoke(cli.app, [*args, "--config", str(self.config)])

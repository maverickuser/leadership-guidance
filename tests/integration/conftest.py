from __future__ import annotations

import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import scarlet_archive.cli as cli
from scarlet_archive.http import Client, RequestsTransport

from ..conftest import TEST_COOKIE
from ..fakes import FakeClock
from .site import FixtureSite

SLUGS = [f"post-{i}" for i in range(1, 9)]
CATEGORIES = {
    "career-growth": ("Career Growth", [["post-1", "post-2"]]),
    "managing-up": ("Managing Up", [["post-3"], ["post-1", "post-4"]]),
}


class Harness:
    def __init__(self, tmp_path: Path, monkeypatch, site: FixtureSite, real_clock: bool = False):
        self.site = site
        self.data = tmp_path / "data"
        self.config = tmp_path / "config.yaml"
        self.config.write_text(f"base_url: {site.base}\noutput_dir: data\n", encoding="utf-8")
        (tmp_path / ".env").write_text(f"SCARLETINK_COOKIE={TEST_COOKIE}\n", encoding="utf-8")
        self.clock = FakeClock()
        self.real_clock = real_clock

        def make_client(cfg):
            if real_clock:
                return Client(cfg, transport=RequestsTransport(), sleep=time.sleep, clock=time.monotonic)
            return Client(cfg, transport=RequestsTransport(), sleep=self.clock.sleep, clock=self.clock.clock)

        monkeypatch.setattr(cli, "make_client", make_client)
        self.runner = CliRunner()

    def run(self, *args: str):
        return self.runner.invoke(cli.app, [*args, "--config", str(self.config)])


@pytest.fixture
def site():
    with FixtureSite(SLUGS, CATEGORIES, TEST_COOKIE) as s:
        yield s


@pytest.fixture
def harness(tmp_path, monkeypatch, site):
    return Harness(tmp_path, monkeypatch, site)

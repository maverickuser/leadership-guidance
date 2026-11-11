from __future__ import annotations

import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

import newsletter_archive.cli as cli
from newsletter_archive.http import Client, RequestsTransport
from newsletter_archive.search.indexer import IndexReport

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
        self.main = self.data / "main"  # the newsletter's own folder
        self.config = tmp_path / "config.yaml"
        self.config.write_text(
            "output_dir: data\nsources:\n  - id: main\n    platform: ghost\n"
            f"    base_url: {site.base}\n    cookie_env: NEWSLETTER_COOKIE\n",
            encoding="utf-8",
        )
        (tmp_path / ".env").write_text(f"NEWSLETTER_COOKIE={TEST_COOKIE}\n", encoding="utf-8")
        self.clock = FakeClock()

        def make_client(cfg, source, limiter):
            sleep, clock = (
                (time.sleep, time.monotonic) if real_clock else (self.clock.sleep, self.clock.clock)
            )
            return Client(
                cfg,
                transport=RequestsTransport(),
                sleep=sleep,
                clock=clock,
                limiter=limiter,
                base_url=source.base_url,
                cookie=source.cookie,
            )

        monkeypatch.setattr(cli, "make_client", make_client)
        # These tests cover the scraping stack; the index stage has its own tests in tests/search.
        monkeypatch.setattr(cli, "index_archive", lambda cfg, options, on_post: IndexReport())
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

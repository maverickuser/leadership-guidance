"""Command-line entry point: archive discover | fetch | extract | run."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Optional

import typer

from . import __version__
from .config import Config, load_config
from .discover import run_discover
from .errors import ArchiveError, ConfigError, MissingCookieError, NotSignedInError
from .fetch import run_fetch
from .http import Client
from .runlog import RunSummary, close_logging, log_post, setup_logging
from .store import load_manifest, run_extract, run_lock

log = logging.getLogger("newsletter_archive.cli")

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Archive the newsletter posts you subscribe to as clean JSON, for personal use.",
)

Limit = Annotated[Optional[int], typer.Option("--limit", min=1, help="Process at most N posts.")]
Refresh = Annotated[bool, typer.Option("--refresh", help="Ignore caches and process again.")]
DryRun = Annotated[
    bool, typer.Option("--dry-run", help="Show what would happen; fetch no posts, write no post files.")
]
Verbose = Annotated[bool, typer.Option("--verbose", "-v", help="Show debug output on the console.")]
ConfigPath = Annotated[
    Path, typer.Option("--config", help="Path to config.yaml (.env is read from the same folder).")
]


def make_client(config: Config) -> Client:
    """Seam for tests: returns the one HTTP client used for a run."""
    return Client(config)


def _execute(
    stages: list[str],
    limit: int | None,
    refresh: bool,
    dry_run: bool,
    verbose: bool,
    config_path: Path,
) -> None:
    try:
        config = load_config(config_path, config_path.parent / ".env")
    except ConfigError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from None

    log_path = setup_logging(config, verbose)
    summary = RunSummary()
    manifest = None
    code = 0
    try:
        if "fetch" in stages and not config.cookie:
            raise MissingCookieError()
        with run_lock(config.lock_path):
            manifest = load_manifest(config.manifest_path)
            client: Client | None = None
            for stage in stages:
                log.info("== %s%s", stage, " (dry run)" if dry_run else "")
                if stage == "discover":
                    client = client or make_client(config)
                    # `run` never limits discovery; `discover --limit N` caps new posts added
                    result = run_discover(
                        client,
                        config,
                        manifest,
                        limit=limit if stages == ["discover"] else None,
                        dry_run=dry_run,
                    )
                    summary.categories_incomplete = bool(result.failed_categories)
                elif stage == "fetch":
                    client = client or make_client(config)
                    fr = run_fetch(client, config, manifest, limit, refresh, dry_run, on_post=log_post)
                    summary.fetched += fr.fetched
                    summary.failed += fr.failed
                    summary.skipped += fr.skipped
                elif stage == "extract":
                    er = run_extract(config, manifest, limit, refresh, dry_run, on_post=log_post)
                    summary.ok += er.ok
                    summary.partial += er.partial
                    summary.failed += er.failed
                    summary.unchanged += er.unchanged
    except NotSignedInError as e:
        log.error(str(e))
        code = e.exit_code
    except ArchiveError as e:
        log.error("Error: %s", e)
        code = e.exit_code
    except KeyboardInterrupt:
        log.warning("Interrupted. Progress so far is saved in the manifest.")
        code = 130
    except Exception:  # unexpected: keep the traceback in the (redacted) log
        log.exception("Unexpected error")
        code = 1
    finally:
        if manifest is not None:
            summary.discovered = len(manifest)
        log.info(summary.line())
        log.info("Log file: %s", log_path)
        close_logging()
    raise typer.Exit(code)


@app.callback(invoke_without_command=True)
def main(
    version: Annotated[bool, typer.Option("--version", help="Show the version and exit.")] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command()
def discover(
    limit: Limit = None,
    dry_run: DryRun = False,
    verbose: Verbose = False,
    config: ConfigPath = Path("config.yaml"),
) -> None:
    """Stage 1: read the sitemap and category pages into data/manifest.json."""
    _execute(["discover"], limit, False, dry_run, verbose, config)


@app.command()
def fetch(
    limit: Limit = None,
    refresh: Refresh = False,
    dry_run: DryRun = False,
    verbose: Verbose = False,
    config: ConfigPath = Path("config.yaml"),
) -> None:
    """Stage 2: download post pages with your cookie into data/raw/."""
    _execute(["fetch"], limit, refresh, dry_run, verbose, config)


@app.command()
def extract(
    limit: Limit = None,
    refresh: Refresh = False,
    dry_run: DryRun = False,
    verbose: Verbose = False,
    config: ConfigPath = Path("config.yaml"),
) -> None:
    """Stages 3-4: turn cached pages into data/posts/{slug}.json (no network)."""
    _execute(["extract"], limit, refresh, dry_run, verbose, config)


@app.command()
def run(
    limit: Limit = None,
    refresh: Refresh = False,
    dry_run: DryRun = False,
    verbose: Verbose = False,
    config: ConfigPath = Path("config.yaml"),
) -> None:
    """All stages in order: discover, fetch, extract."""
    _execute(["discover", "fetch", "extract"], limit, refresh, dry_run, verbose, config)

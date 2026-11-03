"""Command-line entry point: archive discover | fetch | extract | index | run."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Optional

import psycopg
import typer

from . import __version__
from .config import Config, load_config
from .discover import run_discover
from .errors import ArchiveError, ConfigError, MissingCookieError, NotSignedInError
from .fetch import run_fetch
from .http import Client
from .runlog import RunSummary, close_logging, log_post, setup_logging
from .search.indexer import IndexOptions, index_archive
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


INDEX_FAILED_IN_RUN = 3  # `archive run`: indexing failed or a post is stuck (the weekly job notifies)


def _index_stage(config: Config, options: IndexOptions, stages: list[str]) -> int:
    """Run the index stage and log its report. Returns the exit code it contributes.

    Under `archive run`, any index problem becomes exit 3 so the weekly job notifies, while the
    earlier stages' work is kept. Under `archive index`, errors keep their own exit code."""
    in_run = stages != ["index"]
    if in_run and not config.search.enabled:
        log.info("Search is turned off (search.enabled: false); skipping the index stage")
        return 0
    try:
        report = index_archive(config, options, on_post=log_post)
    except (ArchiveError, psycopg.Error) as e:
        if not in_run:
            raise
        log.error("Index step failed: %s", e)
        log.error(
            "New posts are archived. To archive without search, set search.enabled: false in config.yaml"
        )
        return INDEX_FAILED_IN_RUN
    except Exception:  # unexpected, under `run`: keep the traceback, still notify as an index failure
        if not in_run:
            raise
        log.exception("Index step failed unexpectedly")
        return INDEX_FAILED_IN_RUN
    for line in report.lines():
        log.info(line)
    if in_run and (report.stuck or report.breaker_open):
        return INDEX_FAILED_IN_RUN
    return 0


def _execute(
    stages: list[str],
    limit: int | None,
    refresh: bool,
    dry_run: bool,
    verbose: bool,
    config_path: Path,
    index_options: IndexOptions | None = None,
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
                elif stage == "index":
                    code = max(code, _index_stage(config, index_options or IndexOptions(), stages))
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
def index(
    limit: Limit = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be indexed and the estimated cost; change nothing."),
    ] = False,
    retry_failed: Annotated[
        bool, typer.Option("--retry-failed", help="Retry posts that failed 3 times with the same content.")
    ] = False,
    allow_mass_delete: Annotated[
        bool, typer.Option("--allow-mass-delete", help="Allow removing many posts at once.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", help="Go ahead even if over the spending ceiling.")] = False,
    verbose: Verbose = False,
    config: ConfigPath = Path("config.yaml"),
) -> None:
    """Build or update the search store from data/posts/ (needs the database and GEMINI_API_KEY)."""
    options = IndexOptions(limit, dry_run, retry_failed, allow_mass_delete, yes)
    _execute(["index"], limit, False, dry_run, verbose, config, options)


@app.command()
def run(
    limit: Limit = None,
    refresh: Refresh = False,
    dry_run: DryRun = False,
    verbose: Verbose = False,
    config: ConfigPath = Path("config.yaml"),
) -> None:
    """All stages in order: discover, fetch, extract, index. Exit 3 if indexing failed."""
    options = IndexOptions(limit=limit, dry_run=dry_run)
    _execute(["discover", "fetch", "extract", "index"], limit, refresh, dry_run, verbose, config, options)

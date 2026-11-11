"""Command-line entry point: archive discover | fetch | extract | index | run."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Optional

import psycopg
import typer

from . import __version__
from .config import Config, SourceConfig, load_config
from .discover import run_discover
from .errors import ArchiveError, ConfigError, MissingCookieError, NotSignedInError
from .fetch import run_fetch
from .http import Client, RateLimiter
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


def make_client(config: Config, source: SourceConfig, limiter: RateLimiter | None) -> Client:
    """Seam for tests: the HTTP client for one newsletter. Pass the run's limiter so every
    newsletter shares one; with None the client makes it, and later clients reuse it."""
    return Client(config, limiter=limiter, base_url=source.base_url, cookie=source.cookie)


def _refuse_old_layout(config: Config) -> None:
    """Stop if output_dir still has the single-newsletter layout (top-level manifest.json).

    Raises:
        ConfigError: telling the user to run `archive migrate`.
    """
    if config.old_manifest_path.exists():
        raise ConfigError(
            f"{config.output_dir} still has the old single-newsletter layout. "
            "Run `archive migrate --source <id>` first (see the README)."
        )


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


def _load_config(config_path: Path) -> Config:
    """The config and .env beside it, or exit 1 with the problem."""
    try:
        return load_config(config_path, config_path.parent / ".env")
    except ConfigError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from None


def _execute(
    stages: list[str],
    limit: int | None,
    refresh: bool,
    dry_run: bool,
    verbose: bool,
    config_path: Path,
    index_options: IndexOptions | None = None,
) -> None:
    config = _load_config(config_path)
    log_path = setup_logging(config, verbose)
    summary = RunSummary()
    code = 0
    try:
        _refuse_old_layout(config)
        sources = config.sources
        if "fetch" in stages:
            without_cookie = [s for s in sources if not s.cookie]
            if without_cookie:
                raise MissingCookieError(without_cookie[0].cookie_env)
        with run_lock(config.lock_path):
            limiter: RateLimiter | None = None  # one for the whole run, shared by every newsletter
            for source in sources:
                manifest = load_manifest(source.manifest_path)
                client: Client | None = None
                try:
                    for stage in [s for s in stages if s != "index"]:
                        log.info("== %s: %s%s", source.display_name, stage, " (dry run)" if dry_run else "")
                        if stage in ("discover", "fetch") and client is None:
                            client = make_client(config, source, limiter)
                            limiter = client.limiter
                        if stage == "discover":
                            assert client is not None
                            # `run` never limits discovery; `discover --limit N` caps new posts added
                            result = run_discover(
                                client,
                                config,
                                manifest,
                                limit=limit if stages == ["discover"] else None,
                                dry_run=dry_run,
                                source=source,
                            )
                            summary.categories_incomplete |= bool(result.failed_categories)
                        elif stage == "fetch":
                            assert client is not None
                            fr = run_fetch(
                                client,
                                config,
                                manifest,
                                limit,
                                refresh,
                                dry_run,
                                on_post=log_post,
                                source=source,
                            )
                            summary.fetched += fr.fetched
                            summary.failed += fr.failed
                            summary.skipped += fr.skipped
                        elif stage == "extract":
                            er = run_extract(
                                config, manifest, limit, refresh, dry_run, on_post=log_post, source=source
                            )
                            summary.ok += er.ok
                            summary.partial += er.partial
                            summary.failed += er.failed
                            summary.unchanged += er.unchanged
                finally:
                    # Counted even when a newsletter fails part-way, so the summary stays honest.
                    summary.discovered += len(manifest)
            if "index" in stages:
                log.info("== index%s", " (dry run)" if dry_run else "")
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


def _index_flag_problem(
    limit: int | None,
    retry_failed: bool,
    refresh: bool,
    no_swap: bool,
    swap: bool,
    rollback: bool,
    force: bool,
) -> str | None:
    """Why a combination of `archive index` flags makes no sense, or None."""
    if sum((refresh, swap, rollback)) > 1:
        return "use only one of --refresh, --swap or --rollback"
    if no_swap and not refresh:
        return "--no-swap only goes with --refresh"
    if force and not (swap or (refresh and not no_swap)):
        return "--force only goes with --swap or --refresh (it overrides the quality check before a swap)"
    if limit and (swap or rollback):
        return "--limit can't be used with --swap or --rollback: a version goes live only when complete"
    if retry_failed and (refresh or swap or rollback):
        return "--retry-failed is for normal runs; --refresh, --swap and --rollback already try every post"
    return None


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
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Build a new index version (e.g. after changing the model).")
    ] = False,
    no_swap: Annotated[
        bool, typer.Option("--no-swap", help="With --refresh: build it but keep using the current one.")
    ] = False,
    swap: Annotated[bool, typer.Option("--swap", help="Make the newest built index version live.")] = False,
    rollback: Annotated[
        bool, typer.Option("--rollback", help="Go back to the previous index version.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Swap even if the quality check says the new version is worse.")
    ] = False,
    verbose: Verbose = False,
    config: ConfigPath = Path("config.yaml"),
) -> None:
    """Build or update the search store from data/posts/ (needs the database and GEMINI_API_KEY)."""
    problem = _index_flag_problem(limit, retry_failed, refresh, no_swap, swap, rollback, force)
    if problem:
        typer.echo(f"Error: {problem}", err=True)
        raise typer.Exit(1)
    options = IndexOptions(
        limit, dry_run, retry_failed, allow_mass_delete, yes, refresh, no_swap, swap, rollback, force
    )
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


@app.command()
def migrate(
    source: Annotated[
        str,
        typer.Option("--source", help="The id (config.yaml `sources:`) to move the old archive into."),
    ],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would move; change nothing.")] = False,
    verbose: Verbose = False,
    config: ConfigPath = Path("config.yaml"),
) -> None:
    """Move a single-newsletter archive (top-level manifest.json, raw/, posts/) into one
    newsletter's folder, and rename post ids in the eval files. Safe to run again."""
    from .migrate import migrate as run_migrate

    cfg = _load_config(config)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING)
    try:
        result = run_migrate(cfg, source, dry_run=dry_run)
    except (ArchiveError, OSError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from None
    for line in result.lines(cfg.source(source), dry_run):
        typer.echo(line)


@app.command()
def mcp(
    verbose: Verbose = False,
    config: ConfigPath = Path("config.yaml"),
    install_command: Annotated[
        bool,
        typer.Option(
            "--install-command",
            help="Install the Claude Code command /ask-coach (in ~/.claude/commands) and exit.",
        ),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="With --install-command: replace a file archive didn't write.")
    ] = False,
) -> None:
    """Start the local MCP search server (leadership-coach) on stdin/stdout, for Claude Code,
    Claude Desktop and other MCP clients."""
    from . import mcp_server

    if force and not install_command:
        typer.echo("Error: --force only goes with --install-command", err=True)
        raise typer.Exit(2)
    if install_command:  # needs no config, database or secrets
        try:
            path, outcome = mcp_server.install_command(force)
        except (ArchiveError, OSError) as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(1) from None
        said = {"installed": "Installed", "updated": "Updated", "unchanged": "Already up to date:"}[outcome]
        typer.echo(f"{said} /{mcp_server.COMMAND_NAME} ({path})")
        if outcome != "unchanged":
            typer.echo(f"Use it in a new Claude Code session: /{mcp_server.COMMAND_NAME} <your question>")
        return
    cfg = _load_config(config)

    try:
        mcp_server.serve(cfg, verbose)
    except ArchiveError as e:  # e.g. a database password missing from .env
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from None

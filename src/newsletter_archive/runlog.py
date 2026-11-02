"""Run logging: console + per-run log file, cookie redaction, per-post lines, summary."""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

from .config import Config, Secret

LOGGER = "newsletter_archive"
SERVER_LOG_DAYS = 30  # daily server logs (mcp-YYYYMMDD.log) older than this are removed
LIBRARY_LOGGERS = ("psycopg.pool",)  # their warnings (e.g. why a pooled connection failed) go to our log
REDACTED = "[REDACTED]"
COOKIE_LINE_RE = re.compile(r"(?i)\b(set-)?cookie\s*[:=]\s*[^\n]+")
GHOST_COOKIE_RE = re.compile(r"ghost-members-ssr(\.sig)?=[^;\s]+")
GOOGLE_KEY_RE = re.compile(r"AIza[0-9A-Za-z_\-]{35}")
QUIET_STATUSES = {"skipped", "unchanged"}
MIN_SECRET_CHARS = 8

post_log = logging.getLogger(f"{LOGGER}.run")


class RedactFilter(logging.Filter):
    """Scrubs secrets from every record: the cookie (whole value, each name=value pair, Cookie
    lines), any other secret values passed in (API key, database passwords), and anything
    shaped like a Google API key."""

    def __init__(self, secret: Secret, *others: Secret) -> None:
        super().__init__()
        value = secret.reveal()
        pieces = {value} if value else set()
        for pair in value.split(";"):
            if "=" in pair:
                v = pair.split("=", 1)[1].strip()
                if len(v) >= MIN_SECRET_CHARS:
                    pieces.add(v)
        # Same length floor as cookie pieces: a short secret would garble ordinary words.
        pieces.update(o.reveal() for o in others if len(o.reveal()) >= MIN_SECRET_CHARS)
        self._pieces = sorted(pieces, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for piece in self._pieces:
            text = text.replace(piece, REDACTED)
        text = GOOGLE_KEY_RE.sub(REDACTED, text)
        text = GHOST_COOKIE_RE.sub(lambda m: m.group(0).split("=", 1)[0] + "=" + REDACTED, text)
        return COOKIE_LINE_RE.sub(lambda m: f"{'Set-' if m.group(1) else ''}Cookie: {REDACTED}", text)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = self.redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = self.redact(record.stack_info)
        return True


def _log_file(logs_dir: Path, prefix: str = "run") -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = logs_dir / f"{prefix}-{stamp}Z.log"
    n = 2
    while path.exists():
        path = logs_dir / f"{prefix}-{stamp}Z-{n}.log"
        n += 1
    return path


def _remove_handlers(logger: logging.Logger) -> None:
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()


def server_log_file(logs_dir: Path, prefix: str, now: datetime | None = None) -> Path:
    """One log per day for a long-lived server (appended to by every start that day), after
    removing that server's logs older than SERVER_LOG_DAYS."""
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=SERVER_LOG_DAYS)).strftime("%Y%m%d")
    for old in logs_dir.glob(f"{prefix}-*.log"):
        day = old.stem.removeprefix(f"{prefix}-")
        if day.isdigit() and len(day) == 8 and day < cutoff:
            old.unlink(missing_ok=True)
    return logs_dir / f"{prefix}-{now:%Y%m%d}.log"


def setup_logging(
    config: Config, verbose: bool = False, *, stream: TextIO | None = None, prefix: str = "run"
) -> Path:
    """Console (INFO, or DEBUG with --verbose) + DEBUG log file. Returns the log file path.
    ``stream`` defaults to stdout; the MCP server passes stderr (stdout carries the protocol)
    and ``prefix="mcp"`` for a daily log instead of one per run."""
    logger = logging.getLogger(LOGGER)
    _remove_handlers(logger)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    redact = RedactFilter(*config.secrets)

    console = logging.StreamHandler(stream or sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    console.addFilter(redact)
    logger.addHandler(console)

    config.logs_dir.mkdir(parents=True, exist_ok=True)
    path = _log_file(config.logs_dir) if prefix == "run" else server_log_file(config.logs_dir, prefix)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    file_handler.addFilter(redact)
    logger.addHandler(file_handler)
    for name in LIBRARY_LOGGERS:
        library = logging.getLogger(name)
        _remove_handlers(library)
        library.setLevel(logging.WARNING)
        library.propagate = False
        library.addHandler(console)
        library.addHandler(file_handler)
    return path


def close_logging() -> None:
    logger = logging.getLogger(LOGGER)
    _remove_handlers(logger)
    logger.propagate = True
    for name in LIBRARY_LOGGERS:
        library = logging.getLogger(name)
        for h in list(library.handlers):
            library.removeHandler(h)  # closed above with our logger's handlers
        library.propagate = True


def log_post(slug: str, stage: str, status: str, detail: str | None = None) -> None:
    """One line per post per stage: slug, stage, status, and the error if any."""
    line = f"{slug}  {stage}  {status}" + (f"  {detail}" if detail else "")
    post_log.log(logging.DEBUG if status in QUIET_STATUSES else logging.INFO, line)


@dataclass
class RunSummary:
    discovered: int = 0  # posts in the manifest at the end of the run
    fetched: int = 0
    ok: int = 0
    partial: int = 0
    failed: int = 0
    skipped: int = 0
    unchanged: int = 0
    categories_incomplete: bool = False

    def line(self) -> str:
        text = (
            f"Summary: discovered={self.discovered} fetched={self.fetched} ok={self.ok} "
            f"partial={self.partial} failed={self.failed}"
        )
        extras = []
        if self.skipped:
            extras.append(f"skipped={self.skipped}")
        if self.unchanged:
            extras.append(f"unchanged={self.unchanged}")
        if extras:
            text += " (" + " ".join(extras) + ")"
        if self.categories_incomplete:
            text += " [categories incomplete]"
        return text

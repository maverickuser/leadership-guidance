"""Run logging: console + per-run log file, cookie redaction, per-post lines, summary."""

from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import Config, Secret

LOGGER = "scarlet_archive"
REDACTED = "[REDACTED]"
COOKIE_LINE_RE = re.compile(r"(?i)\b(set-)?cookie\s*[:=]\s*[^\n]+")
GHOST_COOKIE_RE = re.compile(r"ghost-members-ssr(\.sig)?=[^;\s]+")
QUIET_STATUSES = {"skipped", "unchanged"}

post_log = logging.getLogger(f"{LOGGER}.run")


class RedactFilter(logging.Filter):
    """Scrubs the cookie (whole value, each name=value pair, Cookie lines) from every record."""

    def __init__(self, secret: Secret) -> None:
        super().__init__()
        value = secret.reveal()
        pieces = {value} if value else set()
        for pair in value.split(";"):
            if "=" in pair:
                v = pair.split("=", 1)[1].strip()
                if len(v) >= 8:
                    pieces.add(v)
        self._pieces = sorted(pieces, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for piece in self._pieces:
            text = text.replace(piece, REDACTED)
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


def _log_file(logs_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = logs_dir / f"run-{stamp}Z.log"
    n = 2
    while path.exists():
        path = logs_dir / f"run-{stamp}Z-{n}.log"
        n += 1
    return path


def _remove_handlers(logger: logging.Logger) -> None:
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()


def setup_logging(config: Config, verbose: bool = False) -> Path:
    """Console (INFO, or DEBUG with --verbose) + DEBUG log file. Returns the log file path."""
    logger = logging.getLogger(LOGGER)
    _remove_handlers(logger)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    redact = RedactFilter(config.cookie)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    console.addFilter(redact)
    logger.addHandler(console)

    config.logs_dir.mkdir(parents=True, exist_ok=True)
    path = _log_file(config.logs_dir)
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    file_handler.addFilter(redact)
    logger.addHandler(file_handler)
    return path


def close_logging() -> None:
    logger = logging.getLogger(LOGGER)
    _remove_handlers(logger)
    logger.propagate = True


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

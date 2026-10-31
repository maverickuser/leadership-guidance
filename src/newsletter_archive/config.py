"""Load settings from config.yaml and the cookie from the environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from . import __version__
from .errors import ConfigError

COOKIE_ENV = "NEWSLETTER_COOKIE"

DEFAULTS: dict[str, Any] = {
    "base_url": "https://www.example.com",
    "delay_seconds": 2,
    "max_retries": 3,
    "timeout_seconds": 30,
    "min_word_count": 300,
    "output_dir": "../newsletter-archive-data",  # next to the repo, outside git
    "user_agent": f"newsletter-archive/{__version__} (personal archive; one request every 2s)",
}

MIN_DELAY_SECONDS = 2
BACKOFF_SECONDS: tuple[int, ...] = (2, 4, 8)  # wait before retry 1, 2, 3
MAX_RETRIES = len(BACKOFF_SECONDS)


class Secret:
    """Holds the cookie. Never shows the value in repr/str."""

    __slots__ = ("_value",)

    def __init__(self, value: str | None):
        self._value = (value or "").strip()

    def reveal(self) -> str:
        return self._value

    def __bool__(self) -> bool:
        return bool(self._value)

    def __repr__(self) -> str:
        return "Secret(****)" if self._value else "Secret(<empty>)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True)
class Config:
    """Validated settings for one run. Build it with load_config()."""

    base_url: str
    delay_seconds: float
    max_retries: int
    timeout_seconds: float
    min_word_count: int
    output_dir: Path
    user_agent: str
    cookie: Secret

    @property
    def manifest_path(self) -> Path:
        return self.output_dir / "manifest.json"

    @property
    def raw_dir(self) -> Path:
        return self.output_dir / "raw"

    @property
    def posts_dir(self) -> Path:
        return self.output_dir / "posts"

    @property
    def logs_dir(self) -> Path:
        return self.output_dir / "logs"

    @property
    def lock_path(self) -> Path:
        return self.output_dir / ".lock"

    def raw_path(self, slug: str) -> Path:
        """Cached HTML for one post."""
        return self.raw_dir / f"{slug}.html"

    def post_path(self, slug: str) -> Path:
        """JSON record for one post."""
        return self.posts_dir / f"{slug}.json"

    def with_overrides(self, **changes: Any) -> Config:
        return _validate(replace(self, **changes))


def clean_cookie(raw: str | None) -> str:
    value = (raw or "").strip()
    if value.lower().startswith("cookie:"):
        value = value[len("cookie:") :].strip()
    # tolerate a value wrapped in quotes
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    return value


def _read_cookie(env_file: Path) -> Secret:
    raw = os.environ.get(COOKIE_ENV)
    if not raw and env_file.is_file():
        raw = dotenv_values(env_file).get(COOKIE_ENV)
    return Secret(clean_cookie(raw))


def _cookie_is_valid(value: str) -> bool:
    """A header value must be printable ASCII on one line."""
    return all(" " <= ch <= "~" for ch in value)


def _validate(cfg: Config) -> Config:
    if cfg.cookie and not _cookie_is_valid(cfg.cookie.reveal()):
        raise ConfigError(
            f"{COOKIE_ENV} contains a line break or non-ASCII character; paste the Cookie header again"
        )
    if cfg.delay_seconds < MIN_DELAY_SECONDS:
        raise ConfigError(f"delay_seconds must be >= {MIN_DELAY_SECONDS}")
    if not 0 <= cfg.max_retries <= MAX_RETRIES:
        raise ConfigError(f"max_retries must be between 0 and {MAX_RETRIES}")
    if cfg.timeout_seconds <= 0:
        raise ConfigError("timeout_seconds must be > 0")
    if cfg.min_word_count < 0:
        raise ConfigError("min_word_count must be >= 0")
    if not cfg.base_url.startswith(("http://", "https://")):
        raise ConfigError("base_url must start with http:// or https://")
    return cfg


def load_config(
    path: Path | str = Path("config.yaml"),
    env_file: Path | str = Path(".env"),
    overrides: dict[str, Any] | None = None,
) -> Config:
    """Read config.yaml (optional) and the cookie, apply overrides, and validate.

    Raises:
        ConfigError: the file can't be parsed, a setting is unknown or out of range,
            or the cookie has characters that can't go in an HTTP header.
    """
    path, env_file = Path(path), Path(env_file)
    values = dict(DEFAULTS)
    if path.is_file():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"could not parse {path}: {e}") from None
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path} must be a mapping of settings")
        unknown = set(loaded) - set(DEFAULTS)
        if unknown:
            raise ConfigError(f"unknown setting(s) in {path}: {', '.join(sorted(unknown))}")
        values.update(loaded)
    if overrides:
        values.update({k: v for k, v in overrides.items() if v is not None})

    base_dir = path.parent if path.is_file() else Path.cwd()
    output_dir = Path(os.path.expanduser(str(values["output_dir"])))
    if not output_dir.is_absolute():
        output_dir = (base_dir / output_dir).resolve()

    try:
        cfg = Config(
            base_url=str(values["base_url"]).rstrip("/"),
            delay_seconds=float(values["delay_seconds"]),
            max_retries=int(values["max_retries"]),
            timeout_seconds=float(values["timeout_seconds"]),
            min_word_count=int(values["min_word_count"]),
            output_dir=output_dir,
            user_agent=str(values["user_agent"]),
            cookie=_read_cookie(env_file),
        )
    except (TypeError, ValueError) as e:
        raise ConfigError(f"invalid setting: {e}") from None
    return _validate(cfg)


__all__ = ["BACKOFF_SECONDS", "COOKIE_ENV", "DEFAULTS", "Config", "Secret", "clean_cookie", "load_config"]

"""Load settings from config.yaml and the cookie from the environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from . import __version__
from .errors import ConfigError

COOKIE_ENV = "NEWSLETTER_COOKIE"
GEMINI_KEY_ENV = "GEMINI_API_KEY"
DB_WRITER_PASSWORD_ENV = "ARCHIVE_DB_WRITER_PASSWORD"
DB_READER_PASSWORD_ENV = "ARCHIVE_DB_READER_PASSWORD"

DEFAULTS: dict[str, Any] = {
    "base_url": None,  # required: the newsletter site, set in config.yaml
    "delay_seconds": 2,
    "max_retries": 3,
    "timeout_seconds": 30,
    "min_word_count": 300,
    "output_dir": "../newsletter-archive-data",  # next to the repo, outside git
    "user_agent": f"newsletter-archive/{__version__} (personal archive; one request every 2s)",
    "search": None,  # optional mapping; see SearchConfig
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
class SummaryConfig:
    """Settings for the one-summary-per-post layer (see openspec add-hybrid-search, lld.md 5.10)."""

    enabled: bool = True
    model: str = "gemini-2.5-flash"  # pinned and verified when the summarizer lands
    max_tokens_per_run: int = 1_500_000


@dataclass(frozen=True)
class SearchConfig:
    """Settings for indexing and hybrid search. Every field can be set under `search:` in config.yaml."""

    embedding_model: str = "gemini-embedding-2"
    embedding_dimension: int = 3072
    embed_batch_size: int = 50
    embed_rpm: int = 300
    embed_tpm: int = 1_000_000
    summary_rpm: int = 60
    summary_tpm: int = 1_000_000
    max_embed_tokens_per_run: int = 1_500_000
    circuit_breaker_failures: int = 3
    cache_retention_days: int = 90
    boilerplate_min_posts: int = 5
    boilerplate_similarity: float = 0.8
    chunk_words: int = 300
    chunk_overlap_words: int = 50
    mass_delete_min: int = 5
    mass_delete_fraction: float = 0.10
    quality_gate_max_recall_drop: float = 2.0
    bm25_boosts: dict[str, float] = field(default_factory=lambda: {"heading": 2.0, "title": 1.5, "body": 1.0})
    candidates: int = 50
    rerank_top: int = 40
    reranker_model: str = "Qwen/Qwen3-Reranker-0.6B"
    rerank_timeout_seconds: float = 10.0
    max_per_post: int = 3
    default_limit: int = 10
    max_limit: int = 30
    db_host: str = "127.0.0.1"
    db_port: int = 5433
    db_name: str = "archive"
    summaries: SummaryConfig = field(default_factory=SummaryConfig)


SEARCH_FIELDS = {f.name for f in fields(SearchConfig)}
SUMMARY_FIELDS = {f.name for f in fields(SummaryConfig)}
BM25_FIELDS = {"heading", "title", "body"}


def _empty_secret() -> Secret:
    return Secret(None)


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
    search: SearchConfig = field(default_factory=SearchConfig)
    gemini_api_key: Secret = field(default_factory=_empty_secret)
    db_writer_password: Secret = field(default_factory=_empty_secret)
    db_reader_password: Secret = field(default_factory=_empty_secret)

    @property
    def secrets(self) -> tuple[Secret, ...]:
        """Every secret value, for log redaction."""
        return (self.cookie, self.gemini_api_key, self.db_writer_password, self.db_reader_password)

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


def _read_env(name: str, env_file: Path) -> str | None:
    """The environment wins over .env."""
    raw = os.environ.get(name)
    if not raw and env_file.is_file():
        raw = dotenv_values(env_file).get(name)
    return raw


def _read_cookie(env_file: Path) -> Secret:
    return Secret(clean_cookie(_read_env(COOKIE_ENV, env_file)))


def _read_secret(name: str, env_file: Path) -> Secret:
    return Secret(_read_env(name, env_file))


def _parse_search(raw: Any) -> SearchConfig:
    """Build SearchConfig from the `search:` mapping; unknown keys and bad types are config errors."""
    if raw is None:
        return SearchConfig()
    if not isinstance(raw, dict):
        raise ConfigError("search must be a mapping of settings")
    unknown = set(raw) - SEARCH_FIELDS
    if unknown:
        raise ConfigError(f"unknown search setting(s): {', '.join(sorted(unknown))}")
    values = dict(raw)
    defaults = SearchConfig()
    summaries = values.pop("summaries", None)
    boosts = values.pop("bm25_boosts", None)
    try:
        for name, value in values.items():
            kind = type(getattr(defaults, name))
            if kind is bool or isinstance(value, bool):
                raise TypeError(f"search.{name} must be a {kind.__name__}")
            if kind is int and isinstance(value, float) and not value.is_integer():
                raise TypeError(f"search.{name} must be a whole number")
            values[name] = kind(value)
        if summaries is not None:
            values["summaries"] = _parse_summaries(summaries)
        if boosts is not None:
            if not isinstance(boosts, dict) or set(boosts) - BM25_FIELDS:
                raise ConfigError("search.bm25_boosts must map heading, title and body to numbers")
            values["bm25_boosts"] = {**defaults.bm25_boosts, **{k: float(v) for k, v in boosts.items()}}
    except (TypeError, ValueError) as e:
        raise ConfigError(f"invalid search setting: {e}") from None
    return SearchConfig(**values)


def _parse_summaries(raw: Any) -> SummaryConfig:
    if not isinstance(raw, dict):
        raise ConfigError("search.summaries must be a mapping of settings")
    unknown = set(raw) - SUMMARY_FIELDS
    if unknown:
        raise ConfigError(f"unknown search.summaries setting(s): {', '.join(sorted(unknown))}")
    values = dict(raw)
    if "enabled" in values and not isinstance(values["enabled"], bool):
        raise ConfigError("search.summaries.enabled must be true or false")
    if "model" in values:
        values["model"] = str(values["model"])
    if "max_tokens_per_run" in values:
        values["max_tokens_per_run"] = int(values["max_tokens_per_run"])
    return SummaryConfig(**values)


def _validate_search(s: SearchConfig) -> None:
    positive = (
        "embedding_dimension", "embed_batch_size", "embed_rpm", "embed_tpm", "summary_rpm",
        "summary_tpm", "max_embed_tokens_per_run", "circuit_breaker_failures", "cache_retention_days",
        "boilerplate_min_posts", "chunk_words", "mass_delete_min", "candidates", "rerank_top",
        "rerank_timeout_seconds", "max_per_post", "default_limit", "max_limit", "db_port",
    )  # fmt: skip
    for name in positive:
        if getattr(s, name) <= 0:
            raise ConfigError(f"search.{name} must be > 0")
    if not 1 <= s.embedding_dimension <= 4000:
        raise ConfigError("search.embedding_dimension must be between 1 and 4000")
    if not 0 <= s.chunk_overlap_words < s.chunk_words:
        raise ConfigError("search.chunk_overlap_words must be >= 0 and smaller than search.chunk_words")
    for name in ("boilerplate_similarity", "mass_delete_fraction"):
        if not 0 < getattr(s, name) <= 1:
            raise ConfigError(f"search.{name} must be between 0 and 1")
    if s.quality_gate_max_recall_drop < 0:
        raise ConfigError("search.quality_gate_max_recall_drop must be >= 0")
    if any(v <= 0 for v in s.bm25_boosts.values()):
        raise ConfigError("search.bm25_boosts values must be > 0")
    if s.rerank_top > 2 * s.candidates:
        raise ConfigError("search.rerank_top can't exceed the combined candidates (2 x search.candidates)")
    if not s.default_limit <= s.max_limit:
        raise ConfigError("search.default_limit must be <= search.max_limit")
    if not s.embedding_model or not s.db_name or not s.db_host:
        raise ConfigError("search.embedding_model, search.db_host and search.db_name must not be empty")
    if s.summaries.max_tokens_per_run <= 0:
        raise ConfigError("search.summaries.max_tokens_per_run must be > 0")
    if s.summaries.enabled and not s.summaries.model:
        raise ConfigError("search.summaries.model must be set when summaries are enabled")


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
    _validate_search(cfg.search)
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

    if not values["base_url"]:
        raise ConfigError(
            f"base_url is not set; copy config.example.yaml to {path} and set the newsletter's address"
        )
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
            search=_parse_search(values["search"]),
            gemini_api_key=_read_secret(GEMINI_KEY_ENV, env_file),
            db_writer_password=_read_secret(DB_WRITER_PASSWORD_ENV, env_file),
            db_reader_password=_read_secret(DB_READER_PASSWORD_ENV, env_file),
        )
    except (TypeError, ValueError) as e:
        raise ConfigError(f"invalid setting: {e}") from None
    return _validate(cfg)


__all__ = [
    "BACKOFF_SECONDS",
    "COOKIE_ENV",
    "DB_READER_PASSWORD_ENV",
    "DB_WRITER_PASSWORD_ENV",
    "DEFAULTS",
    "GEMINI_KEY_ENV",
    "Config",
    "SearchConfig",
    "Secret",
    "SummaryConfig",
    "clean_cookie",
    "load_config",
]

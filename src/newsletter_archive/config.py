"""Load settings from config.yaml and each newsletter's cookie from the environment / .env."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from . import __version__
from .errors import ConfigError
from .platforms import PLATFORM_NAMES
from .search.text import squash

COOKIE_ENV = "NEWSLETTER_COOKIE"
GEMINI_KEY_ENV = "GEMINI_API_KEY"
DB_WRITER_PASSWORD_ENV = "ARCHIVE_DB_WRITER_PASSWORD"
DB_READER_PASSWORD_ENV = "ARCHIVE_DB_READER_PASSWORD"
DB_PORT_ENV = "ARCHIVE_DB_PORT"

DEFAULTS: dict[str, Any] = {
    "sources": None,  # required: the newsletters, set in config.yaml
    "delay_seconds": 2,
    "max_retries": 3,
    "timeout_seconds": 30,
    "min_word_count": 300,
    "output_dir": "../leadership-coach-datasets",  # next to the repo, outside git
    "user_agent": f"newsletter-archive/{__version__} (personal archive; one request every 2s)",
    "search": None,  # optional mapping; see SearchConfig
}

SOURCE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
COOKIE_ENV_RE = re.compile(r"[A-Z][A-Z0-9_]*")
# Variables that hold other secrets; a source's cookie_env may never point at one of them.
NON_COOKIE_ENVS = frozenset({GEMINI_KEY_ENV, DB_WRITER_PASSWORD_ENV, DB_READER_PASSWORD_ENV, DB_PORT_ENV})
# Names a source folder can't take: shared items at the top of output_dir, and the folders of
# the old single-newsletter layout (so `archive migrate` can never collide with a source).
RESERVED_SOURCE_IDS = ("logs", "models", "postgres", "posts", "raw")
SOURCE_FIELDS = {"id", "name", "platform", "base_url", "cookie_env", "boilerplate_prefixes"}
SOURCE_REQUIRED = ("id", "platform", "base_url", "cookie_env")

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
    model: str = "gemini-3.8-flash"  # pinned in PR 4: newest stable Flash, not a "latest" alias
    thinking_level: str = "low"  # "" sends no thinking setting (for models without it)
    max_tokens_per_run: int = 1_500_000


@dataclass(frozen=True)
class SearchConfig:
    """Settings for indexing and hybrid search. Every field can be set under `search:` in config.yaml."""

    enabled: bool = True  # false: `archive run` skips the index stage (archive only)
    embedding_model: str = "gemini-embedding-2"
    embedding_dimension: int = 3072
    embed_batch_size: int = 50
    embed_rpm: int = 300
    embed_tpm: int = 1_000_000
    summary_rpm: int = 60
    summary_tpm: int = 1_000_000
    max_embed_tokens_per_run: int = 1_500_000
    # Prices for the cost estimate (USD per million tokens); set them to your tier's rates.
    embed_usd_per_million_tokens: float = 0.20  # gemini-embedding-2, paid tier (Nov 2026)
    summary_usd_per_million_tokens: float = 0.75  # gemini-3.8-flash input rate (Nov 2026)
    gemini_timeout_seconds: float = 60.0
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
    # RRF weight per list; semantic 2 measured best on the evaluation (PR 10)
    fusion_weights: dict[str, float] = field(default_factory=lambda: {"keyword": 1.0, "semantic": 2.0})
    candidates: int = 50
    rerank_top: int = 20  # ~0.22 s per pair on Apple MPS (measured in PR 8)
    reranker_model: str = "Qwen/Qwen3-Reranker-0.6B"
    rerank_timeout_seconds: float = 10.0
    query_timeout_seconds: float = 8.0  # embedding a search query (search falls back to keywords)
    max_per_post: int = 3
    # Searches over every newsletter show each one that has a relevant result: a newsletter
    # missing from the top gets its best result in if its re-ranker score is at least this
    # (a probability, 0-1). 0.5 = "the re-ranker judges it relevant"; provisional until it is
    # measured on the real two-newsletter index (tasks.md 7.4).
    source_min_score: float = 0.5
    default_limit: int = 10
    max_limit: int = 30
    db_host: str = "127.0.0.1"
    db_port: int = 5433
    db_name: str = "archive"
    summaries: SummaryConfig = field(default_factory=SummaryConfig)


SEARCH_FIELDS = {f.name for f in fields(SearchConfig)}
SUMMARY_FIELDS = {f.name for f in fields(SummaryConfig)}
BM25_FIELDS = {"heading", "title", "body"}
THINKING_LEVELS = ("", "minimal", "low", "medium", "high")


def _empty_secret() -> Secret:
    return Secret(None)


@dataclass(frozen=True)
class SourceConfig:
    """One newsletter: its site, platform, cookie, and its own folder under output_dir."""

    id: str
    platform: str
    base_url: str
    cookie_env: str
    cookie: Secret
    root: Path  # output_dir / id
    name: str = ""  # display name; the id when empty
    boilerplate_prefixes: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        return self.name or self.id

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def posts_dir(self) -> Path:
        return self.root / "posts"

    def raw_path(self, slug: str) -> Path:
        """Cached HTML for one post."""
        return self.raw_dir / f"{slug}.html"

    def post_path(self, slug: str) -> Path:
        """JSON record for one post."""
        return self.posts_dir / f"{slug}.json"

    def post_id(self, slug: str) -> str:
        """The post's id everywhere: in its file, the search store and the eval files."""
        return f"{self.id}/{slug}"


@dataclass(frozen=True)
class Config:
    """Validated settings for one run. Build it with load_config()."""

    sources: tuple[SourceConfig, ...]
    delay_seconds: float
    max_retries: int
    timeout_seconds: float
    min_word_count: int
    output_dir: Path
    user_agent: str
    search: SearchConfig = field(default_factory=SearchConfig)
    gemini_api_key: Secret = field(default_factory=_empty_secret)
    db_writer_password: Secret = field(default_factory=_empty_secret)
    db_reader_password: Secret = field(default_factory=_empty_secret)

    @property
    def secrets(self) -> tuple[Secret, ...]:
        """Every secret value, for log redaction."""
        cookies = tuple(s.cookie for s in self.sources)
        return (*cookies, self.gemini_api_key, self.db_writer_password, self.db_reader_password)

    @property
    def old_manifest_path(self) -> Path:
        """Where the single-newsletter layout kept its manifest (see `archive migrate`)."""
        return self.output_dir / "manifest.json"

    def source(self, source_id: str) -> SourceConfig:
        """The source with this id.

        Raises:
            ConfigError: no such source; the message lists the valid ids.
        """
        for s in self.sources:
            if s.id == source_id:
                return s
        valid = ", ".join(s.id for s in self.sources)
        raise ConfigError(f"unknown source {source_id!r}; valid sources: {valid}")

    def select(self, source_ids: list[str] | None) -> tuple[SourceConfig, ...]:
        """The sources named, in config order; every source when none are named.

        Raises:
            ConfigError: a name isn't a configured source.
        """
        if not source_ids:
            return self.sources
        wanted = {self.source(i).id for i in source_ids}
        return tuple(s for s in self.sources if s.id in wanted)

    def only_source(self) -> SourceConfig:
        """The single configured source, for callers that don't name one.

        Raises:
            ConfigError: several sources are configured, so the caller must name one.
        """
        if len(self.sources) != 1:
            raise ConfigError("several sources are configured; name the one to use")
        return self.sources[0]

    @property
    def logs_dir(self) -> Path:
        return self.output_dir / "logs"

    @property
    def lock_path(self) -> Path:
        return self.output_dir / ".lock"

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


def _env_reader(env_file: Path) -> Callable[[str], str | None]:
    """Look up a variable: the environment wins over .env. The file is parsed once."""
    file_values = dotenv_values(env_file) if env_file.is_file() else {}

    def read(name: str) -> str | None:
        return os.environ.get(name) or file_values.get(name)

    return read


def _coerce(label: str, value: Any, default: Any) -> Any:
    """Convert one setting to its default's type, refusing nulls, bools-as-numbers and
    fractional whole numbers."""
    kind = type(default)
    if value is None:
        raise ConfigError(f"{label} must not be empty")
    if kind is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{label} must be true or false")
        return value
    if isinstance(value, bool):
        raise ConfigError(f"{label} must be a {kind.__name__}")
    if kind is int and isinstance(value, float) and not value.is_integer():
        raise ConfigError(f"{label} must be a whole number")
    if kind is str and not isinstance(value, str | int | float):
        raise ConfigError(f"{label} must be text")
    try:
        return kind(value)
    except (TypeError, ValueError):
        raise ConfigError(f"invalid search setting: {label} must be a {kind.__name__}") from None


def _section(label: str, raw: Any, defaults: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} must be a mapping of settings")
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"unknown {label} setting(s): {', '.join(sorted(unknown))}")
    return dict(raw)


def _parse_search(raw: Any, env_port: str | None = None) -> SearchConfig:
    """Build SearchConfig from the `search:` mapping; unknown keys and bad types are config errors.

    ``env_port`` (ARCHIVE_DB_PORT, also read by docker compose) sets the port unless
    ``search.db_port`` is given.
    """
    defaults = SearchConfig()
    values = {} if raw is None else _section("search", raw, defaults, SEARCH_FIELDS)
    if env_port and "db_port" not in values:
        values["db_port"] = env_port
    summaries = values.pop("summaries", None)
    boosts = values.pop("bm25_boosts", None)
    weights = values.pop("fusion_weights", None)
    for name, value in values.items():
        values[name] = _coerce(f"search.{name}", value, getattr(defaults, name))
    if summaries is not None:
        sv = _section("search.summaries", summaries, defaults.summaries, SUMMARY_FIELDS)
        parsed = {
            k: _coerce(f"search.summaries.{k}", v, getattr(defaults.summaries, k)) for k, v in sv.items()
        }
        if "thinking_level" in parsed:
            parsed["thinking_level"] = parsed["thinking_level"].strip().lower()
        values["summaries"] = SummaryConfig(**parsed)
    if boosts is not None:
        values["bm25_boosts"] = _number_map(
            "bm25_boosts", boosts, defaults.bm25_boosts, "heading, title and body"
        )
    if weights is not None:
        values["fusion_weights"] = _number_map(
            "fusion_weights", weights, defaults.fusion_weights, "keyword and semantic"
        )
    return SearchConfig(**values)


def _number_map(name: str, raw: Any, defaults: dict[str, float], keys: str) -> dict[str, float]:
    """A ``{key: number}`` setting merged over its defaults (bm25_boosts, fusion_weights)."""
    if not isinstance(raw, dict) or set(raw) - set(defaults):
        raise ConfigError(f"search.{name} must map {keys} to numbers")
    return {**defaults, **{k: _coerce(f"search.{name}.{k}", v, 1.0) for k, v in raw.items()}}


def _parse_prefixes(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(p, str) and p.strip() for p in raw):
        raise ConfigError("boilerplate_prefixes must be a list of non-empty strings")
    return tuple(squash(p) for p in raw)


def _validate_search(s: SearchConfig) -> None:
    positive = (
        "embedding_dimension", "embed_batch_size", "embed_rpm", "embed_tpm", "summary_rpm",
        "summary_tpm", "max_embed_tokens_per_run", "circuit_breaker_failures", "cache_retention_days",
        "boilerplate_min_posts", "chunk_words", "mass_delete_min", "candidates", "rerank_top",
        "rerank_timeout_seconds", "query_timeout_seconds", "max_per_post", "default_limit", "max_limit",
        "db_port",
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
    for name in ("embed_usd_per_million_tokens", "summary_usd_per_million_tokens"):
        if getattr(s, name) < 0:
            raise ConfigError(f"search.{name} must be >= 0")
    if not 0 <= s.source_min_score <= 1:
        raise ConfigError("search.source_min_score must be between 0 and 1")
    if s.gemini_timeout_seconds <= 0:
        raise ConfigError("search.gemini_timeout_seconds must be > 0")
    if s.quality_gate_max_recall_drop < 0:
        raise ConfigError("search.quality_gate_max_recall_drop must be >= 0")
    for name in ("bm25_boosts", "fusion_weights"):
        if not all(math.isfinite(v) and v > 0 for v in getattr(s, name).values()):
            raise ConfigError(f"search.{name} values must be positive numbers")
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
    if s.summaries.thinking_level not in THINKING_LEVELS:
        raise ConfigError("search.summaries.thinking_level must be one of: '', minimal, low, medium, high")


def _old_config_message(path: Path, loaded: dict[str, Any]) -> str:
    """The error for a single-newsletter config: the `sources:` entry to paste in instead."""
    lines = [
        "sources:",
        "  - id: <choose a short id, e.g. main>   # names the newsletter's folder",
        f"    base_url: {loaded.get('base_url') or 'https://...'}",
        "    platform: ghost",
        f"    cookie_env: {COOKIE_ENV}",
    ]
    prefixes = loaded.get("boilerplate_prefixes")
    if isinstance(prefixes, list) and prefixes:
        lines.append("    boilerplate_prefixes:")
        lines += [f"      - {p!r}" for p in prefixes]
    entry = "\n".join(lines)
    return (
        f"{path} uses the old single-newsletter settings (base_url). Each newsletter is now an "
        f"entry under `sources:`. Replace base_url"
        f"{' and boilerplate_prefixes' if 'boilerplate_prefixes' in loaded else ''} with:\n\n{entry}\n\n"
        "Then move your data with `archive migrate --source <id>` (see the README)."
    )


def _parse_sources(
    raw: Any, output_dir: Path, read_env: Callable[[str], str | None]
) -> tuple[SourceConfig, ...]:
    """Build and check the `sources:` list.

    Raises:
        ConfigError: the list is missing or empty, or a source is invalid (the message names it).
    """
    if not isinstance(raw, list) or not raw:
        raise ConfigError(
            "sources is not set; copy config.example.yaml to config.yaml and add each newsletter "
            "under `sources:`"
        )
    sources: list[SourceConfig] = []
    for i, item in enumerate(raw, start=1):
        where = f"sources entry {i}"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} must be a mapping of settings")
        unknown = set(item) - SOURCE_FIELDS
        if unknown:
            raise ConfigError(f"unknown setting(s) in {where}: {', '.join(sorted(unknown))}")
        missing = [k for k in SOURCE_REQUIRED if not item.get(k)]
        if missing:
            raise ConfigError(f"{where} needs {', '.join(missing)}")
        sid = str(item["id"])
        where = f"source {sid!r}"
        if not SOURCE_ID_RE.fullmatch(sid):
            raise ConfigError(f"{where}: id must be lowercase letters, digits and hyphens")
        if sid in RESERVED_SOURCE_IDS:
            raise ConfigError(
                f"{where}: id can't be one of {', '.join(RESERVED_SOURCE_IDS)} (used for shared data)"
            )
        platform = str(item["platform"])
        if platform not in PLATFORM_NAMES:
            raise ConfigError(
                f"{where}: unknown platform {platform!r}; use one of: {', '.join(PLATFORM_NAMES)}"
            )
        base_url = str(item["base_url"]).rstrip("/")
        if not base_url.startswith(("http://", "https://")) or not _site_host(base_url):
            raise ConfigError(f"{where}: base_url must start with http:// or https:// and name a site")
        cookie_env = str(item["cookie_env"])
        if not COOKIE_ENV_RE.fullmatch(cookie_env):
            raise ConfigError(f"{where}: cookie_env must be an environment variable name like {COOKIE_ENV}")
        if cookie_env in NON_COOKIE_ENVS:
            # Never send the API key or a database password to a website as a cookie.
            raise ConfigError(f"{where}: cookie_env {cookie_env} holds another secret, not a cookie")
        cookie = Secret(clean_cookie(read_env(cookie_env)))
        if cookie and not _cookie_is_valid(cookie.reveal()):
            raise ConfigError(
                f"{cookie_env} contains a line break or non-ASCII character; paste the Cookie header again"
            )
        name = item.get("name")
        if name is not None and not (isinstance(name, str) and name.strip()):
            raise ConfigError(f"{where}: name must be non-empty text")
        try:
            prefixes = _parse_prefixes(item.get("boilerplate_prefixes"))
        except ConfigError as e:
            raise ConfigError(f"{where}: {e}") from None
        sources.append(
            SourceConfig(
                id=sid,
                platform=platform,
                base_url=base_url,
                cookie_env=cookie_env,
                cookie=cookie,
                root=output_dir / sid,
                name=(name or "").strip(),
                boilerplate_prefixes=prefixes,
            )
        )
    return tuple(sources)


def _site_host(base_url: str) -> str:
    """The host part of a site URL, lowercased (`https://www.Example.com/x` -> `www.example.com`)."""
    return base_url.split("://", 1)[-1].split("/", 1)[0].lower()


def _check_sources(sources: tuple[SourceConfig, ...]) -> None:
    """Ids, sites and cookie variables must be unique across sources: one site gets one cookie,
    and a cookie goes to one site."""
    seen_ids: set[str] = set()
    seen_sites: dict[str, str] = {}
    seen_cookie_envs: dict[str, str] = {}
    for s in sources:
        if s.id in seen_ids:
            raise ConfigError(f"duplicate source id {s.id!r}")
        seen_ids.add(s.id)
        host = _site_host(s.base_url)  # http and https count as the same site
        if host in seen_sites:
            raise ConfigError(f"sources {seen_sites[host]!r} and {s.id!r} have the same base_url site")
        seen_sites[host] = s.id
        if s.cookie_env in seen_cookie_envs:
            raise ConfigError(
                f"sources {seen_cookie_envs[s.cookie_env]!r} and {s.id!r} share cookie_env "
                f"{s.cookie_env}; each newsletter needs its own cookie variable"
            )
        seen_cookie_envs[s.cookie_env] = s.id


def _cookie_is_valid(value: str) -> bool:
    """A header value must be printable ASCII on one line."""
    return all(" " <= ch <= "~" for ch in value)


def _validate(cfg: Config) -> Config:
    _check_sources(cfg.sources)
    if cfg.delay_seconds < MIN_DELAY_SECONDS:
        raise ConfigError(f"delay_seconds must be >= {MIN_DELAY_SECONDS}")
    if not 0 <= cfg.max_retries <= MAX_RETRIES:
        raise ConfigError(f"max_retries must be between 0 and {MAX_RETRIES}")
    if cfg.timeout_seconds <= 0:
        raise ConfigError("timeout_seconds must be > 0")
    if cfg.min_word_count < 0:
        raise ConfigError("min_word_count must be >= 0")
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
        if "base_url" in loaded and "sources" not in loaded:
            raise ConfigError(_old_config_message(path, loaded))
        unknown = set(loaded) - set(DEFAULTS)
        if unknown:
            raise ConfigError(f"unknown setting(s) in {path}: {', '.join(sorted(unknown))}")
        values.update(loaded)
    if overrides:
        values.update({k: v for k, v in overrides.items() if v is not None})

    read_env = _env_reader(env_file)
    base_dir = path.parent if path.is_file() else Path.cwd()
    output_dir = Path(os.path.expanduser(str(values["output_dir"])))
    if not output_dir.is_absolute():
        output_dir = (base_dir / output_dir).resolve()

    sources = _parse_sources(values["sources"], output_dir, read_env)
    try:
        cfg = Config(
            sources=sources,
            delay_seconds=float(values["delay_seconds"]),
            max_retries=int(values["max_retries"]),
            timeout_seconds=float(values["timeout_seconds"]),
            min_word_count=int(values["min_word_count"]),
            output_dir=output_dir,
            user_agent=str(values["user_agent"]),
            search=_parse_search(values["search"], read_env(DB_PORT_ENV)),
            gemini_api_key=Secret(read_env(GEMINI_KEY_ENV)),
            db_writer_password=Secret(read_env(DB_WRITER_PASSWORD_ENV)),
            db_reader_password=Secret(read_env(DB_READER_PASSWORD_ENV)),
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
    "SourceConfig",
    "SummaryConfig",
    "clean_cookie",
    "load_config",
]

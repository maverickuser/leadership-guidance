import pytest

from newsletter_archive.config import DEFAULTS, Secret, clean_cookie, load_config
from newsletter_archive.errors import ConfigError

from .conftest import TEST_COOKIE

SITE = {"base_url": "https://www.example.com"}


def write(path, text):
    """Write a config file; every config needs base_url, so add it unless the test sets one."""
    if path.suffix == ".yaml" and "base_url" not in text:
        text = "base_url: https://www.example.com\n" + text
    path.write_text(text, encoding="utf-8")
    return path


def test_base_url_is_required(tmp_path):
    assert DEFAULTS["base_url"] is None
    with pytest.raises(ConfigError, match="base_url is not set"):
        load_config(tmp_path / "missing.yaml", tmp_path / ".env")


def test_defaults_when_no_config_file(tmp_path):
    cfg = load_config(tmp_path / "missing.yaml", tmp_path / ".env", overrides=SITE)
    assert cfg.base_url == "https://www.example.com"
    assert cfg.delay_seconds == 2
    assert cfg.max_retries == 3
    assert cfg.timeout_seconds == 30
    assert cfg.min_word_count == 300
    assert cfg.output_dir.name == "newsletter-archive-data"
    assert not cfg.cookie


def test_yaml_overrides_defaults_and_relative_output_dir(tmp_path):
    p = write(tmp_path / "config.yaml", "delay_seconds: 5\noutput_dir: out\nbase_url: https://x.test/\n")
    cfg = load_config(p, tmp_path / ".env")
    assert cfg.delay_seconds == 5
    assert cfg.output_dir == (tmp_path / "out").resolve()
    assert cfg.base_url == "https://x.test"
    assert cfg.manifest_path == cfg.output_dir / "manifest.json"
    assert cfg.raw_dir.name == "raw" and cfg.posts_dir.name == "posts" and cfg.logs_dir.name == "logs"


def test_absolute_and_home_output_dir(tmp_path):
    p = write(tmp_path / "config.yaml", f"output_dir: {tmp_path / 'abs'}\n")
    assert load_config(p, tmp_path / ".env").output_dir == tmp_path / "abs"
    p = write(tmp_path / "config.yaml", "output_dir: ~/somewhere\n")
    assert str(load_config(p, tmp_path / ".env").output_dir).startswith(str(tmp_path.home()))


def test_cli_overrides_beat_yaml(tmp_path):
    p = write(tmp_path / "config.yaml", "delay_seconds: 5\n")
    cfg = load_config(p, tmp_path / ".env", overrides={"delay_seconds": 3, "max_retries": None})
    assert cfg.delay_seconds == 3 and cfg.max_retries == 3


def test_unknown_key_rejected(tmp_path):
    p = write(tmp_path / "config.yaml", "delay: 5\n")
    with pytest.raises(ConfigError, match="unknown setting"):
        load_config(p, tmp_path / ".env")


@pytest.mark.parametrize(
    "yaml_text, msg",
    [
        ("delay_seconds: 1\n", "delay_seconds"),
        ("max_retries: 4\n", "max_retries"),
        ("timeout_seconds: 0\n", "timeout_seconds"),
        ("base_url: ftp://x\n", "base_url"),
        ("delay_seconds: abc\n", "invalid setting"),
    ],
)
def test_invalid_values_rejected(tmp_path, yaml_text, msg):
    p = write(tmp_path / "config.yaml", yaml_text)
    with pytest.raises(ConfigError, match=msg):
        load_config(p, tmp_path / ".env")


def test_with_overrides_revalidates(cfg):
    with pytest.raises(ConfigError):
        cfg.with_overrides(delay_seconds=0.5)


def test_cookie_from_env_file(tmp_path):
    env = write(tmp_path / ".env", f"NEWSLETTER_COOKIE={TEST_COOKIE}\n")
    assert load_config(tmp_path / "none.yaml", env, overrides=SITE).cookie.reveal() == TEST_COOKIE


def test_environment_variable_beats_env_file(tmp_path, monkeypatch):
    env = write(tmp_path / ".env", "NEWSLETTER_COOKIE=from-file\n")
    monkeypatch.setenv("NEWSLETTER_COOKIE", "from-env")
    assert load_config(tmp_path / "none.yaml", env, overrides=SITE).cookie.reveal() == "from-env"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Cookie: a=1; b=2", "a=1; b=2"),
        ("cookie:a=1", "a=1"),
        ('  "a=1; b=2"  ', "a=1; b=2"),
        ("", ""),
        (None, ""),
    ],
)
def test_clean_cookie(raw, expected):
    assert clean_cookie(raw) == expected


def test_secret_never_shows_value(cfg):
    assert "TESTSECRET123" not in repr(cfg)
    assert "TESTSECRET123" not in str(cfg.cookie)
    assert repr(Secret("")) == "Secret(<empty>)"
    assert not Secret("   ")
    assert Secret("x") == Secret("x")


# --- search settings and secrets (add-hybrid-search) -------------------------------------------


def test_search_defaults(tmp_path):
    cfg = load_config(tmp_path / "missing.yaml", tmp_path / ".env", overrides=SITE)
    s = cfg.search
    assert (s.embedding_model, s.embedding_dimension) == ("gemini-embedding-2", 3072)
    assert (s.chunk_words, s.chunk_overlap_words, s.candidates, s.rerank_top) == (300, 50, 50, 40)
    assert s.bm25_boosts == {"heading": 2.0, "title": 1.5, "body": 1.0}
    assert (s.db_host, s.db_port, s.db_name) == ("127.0.0.1", 5433, "archive")
    assert s.summaries.enabled and s.summaries.max_tokens_per_run == 1_500_000
    assert not cfg.gemini_api_key and not cfg.db_writer_password and not cfg.db_reader_password


def test_search_overrides_and_nested_sections(tmp_path):
    p = write(
        tmp_path / "config.yaml",
        "search:\n  chunk_words: 400\n  chunk_overlap_words: 100\n  boilerplate_similarity: 0.9\n"
        "  rerank_timeout_seconds: 5\n  db_port: 6543\n  embedding_dimension: 1536.0\n"
        "  bm25_boosts: {title: 1}\n"
        "  summaries: {enabled: false, model: other-model, max_tokens_per_run: 1000}\n",
    )
    s = load_config(p, tmp_path / ".env").search
    assert (s.chunk_words, s.chunk_overlap_words, s.db_port, s.embedding_dimension) == (400, 100, 6543, 1536)
    assert s.boilerplate_similarity == 0.9 and s.rerank_timeout_seconds == 5.0
    assert s.bm25_boosts == {"heading": 2.0, "title": 1.0, "body": 1.0}
    assert not s.summaries.enabled and s.summaries.model == "other-model"
    assert s.summaries.max_tokens_per_run == 1000


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("search: [1]\n", "search must be a mapping"),
        ("search:\n  chunk_size: 3\n", "unknown search setting"),
        ("search:\n  chunk_words: lots\n", "invalid search setting"),
        ("search:\n  chunk_words: true\n", "must be a int"),
        ("search:\n  chunk_words: 2.5\n", "whole number"),
        ("search:\n  chunk_words: 0\n", "chunk_words must be > 0"),
        ("search:\n  embedding_dimension: 5000\n", "between 1 and 4000"),
        ("search:\n  chunk_overlap_words: 300\n", "smaller than search.chunk_words"),
        ("search:\n  chunk_overlap_words: -1\n", "smaller than search.chunk_words"),
        ("search:\n  boilerplate_similarity: 1.5\n", "between 0 and 1"),
        ("search:\n  mass_delete_fraction: 0\n", "between 0 and 1"),
        ("search:\n  quality_gate_max_recall_drop: -1\n", "recall_drop must be >= 0"),
        ("search:\n  bm25_boosts: {subtitle: 2}\n", "bm25_boosts must map"),
        ("search:\n  bm25_boosts: [2]\n", "bm25_boosts must map"),
        ("search:\n  bm25_boosts: {body: 0}\n", "boosts values must be > 0"),
        ("search:\n  rerank_top: 101\n", "rerank_top can't exceed"),
        ("search:\n  default_limit: 40\n", "default_limit must be <="),
        ("search:\n  db_name: ''\n", "must not be empty"),
        ("search:\n  summaries: yes\n", "summaries must be a mapping"),
        ("search:\n  summaries: {mode: x}\n", "unknown search.summaries"),
        ("search:\n  summaries: {enabled: 1}\n", "enabled must be true or false"),
        ("search:\n  summaries: {max_tokens_per_run: 0}\n", "max_tokens_per_run must be > 0"),
        ("search:\n  summaries: {model: ''}\n", "model must be set"),
        ("search:\n  summaries: {model: ~}\n", "summaries.model must not be empty"),
        ("search:\n  summaries: {thinking_level: extreme}\n", "thinking_level must be one of"),
        ("search:\n  summaries: {max_tokens_per_run: 2.5}\n", "whole number"),
        ("search:\n  summaries: {max_tokens_per_run: true}\n", "must be a int"),
        ("search:\n  db_host: ~\n", "db_host must not be empty"),
        ("search:\n  db_host: [a]\n", "db_host must be text"),
        ("search:\n  boilerplate_similarity: yes\n", "must be a float"),
        ("search:\n  bm25_boosts: {body: high}\n", "bm25_boosts.body must be a float"),
    ],
)
def test_invalid_search_settings(tmp_path, yaml_text, message):
    p = write(tmp_path / "config.yaml", yaml_text)
    with pytest.raises(ConfigError, match=message):
        load_config(p, tmp_path / ".env")


def test_db_port_from_environment_unless_set_in_yaml(tmp_path, monkeypatch):
    env = write(tmp_path / ".env", "ARCHIVE_DB_PORT=6000\n")
    assert load_config(tmp_path / "missing.yaml", env, overrides=SITE).search.db_port == 6000
    monkeypatch.setenv("ARCHIVE_DB_PORT", "6001")
    assert load_config(tmp_path / "missing.yaml", env, overrides=SITE).search.db_port == 6001
    p = write(tmp_path / "config.yaml", "search:\n  db_port: 7000\n")
    assert load_config(p, env).search.db_port == 7000


def test_search_secrets_from_env_file_and_environment(tmp_path, monkeypatch):
    from .conftest import TEST_DB_PASSWORD, TEST_GEMINI_KEY

    env = write(
        tmp_path / ".env",
        f"GEMINI_API_KEY={TEST_GEMINI_KEY}\nARCHIVE_DB_WRITER_PASSWORD={TEST_DB_PASSWORD}\n",
    )
    monkeypatch.setenv("ARCHIVE_DB_READER_PASSWORD", "from-environment")
    cfg = load_config(tmp_path / "missing.yaml", env, overrides=SITE)
    assert cfg.gemini_api_key.reveal() == TEST_GEMINI_KEY
    assert cfg.db_writer_password.reveal() == TEST_DB_PASSWORD
    assert cfg.db_reader_password.reveal() == "from-environment"
    shown = repr(cfg)
    assert TEST_GEMINI_KEY not in shown and TEST_DB_PASSWORD not in shown
    assert len(cfg.secrets) == 4


def test_boilerplate_prefixes(tmp_path):
    p = write(tmp_path / "config.yaml", "boilerplate_prefixes:\n  - '  Hello   there '\n")
    assert load_config(p, tmp_path / ".env").boilerplate_prefixes == ("Hello there",)
    assert (
        load_config(tmp_path / "missing.yaml", tmp_path / ".env", overrides=SITE).boilerplate_prefixes == ()
    )
    for bad in (
        "boilerplate_prefixes: hello\n",
        "boilerplate_prefixes: ['']\n",
        "boilerplate_prefixes: [3]\n",
    ):
        p = write(tmp_path / "config.yaml", bad)
        with pytest.raises(ConfigError, match="boilerplate_prefixes"):
            load_config(p, tmp_path / ".env")

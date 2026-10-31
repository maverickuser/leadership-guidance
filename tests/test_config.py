import pytest

from newsletter_archive.config import DEFAULTS, Secret, clean_cookie, load_config
from newsletter_archive.errors import ConfigError

from .conftest import TEST_COOKIE


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_when_no_config_file(tmp_path):
    cfg = load_config(tmp_path / "missing.yaml", tmp_path / ".env")
    assert cfg.base_url == DEFAULTS["base_url"]
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
    assert load_config(tmp_path / "none.yaml", env).cookie.reveal() == TEST_COOKIE


def test_environment_variable_beats_env_file(tmp_path, monkeypatch):
    env = write(tmp_path / ".env", "NEWSLETTER_COOKIE=from-file\n")
    monkeypatch.setenv("NEWSLETTER_COOKIE", "from-env")
    assert load_config(tmp_path / "none.yaml", env).cookie.reveal() == "from-env"


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

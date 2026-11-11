"""The `sources:` list: one entry per newsletter, each with its own folder and cookie."""

from __future__ import annotations

import pytest

from newsletter_archive.config import load_config
from newsletter_archive.errors import ConfigError

from .conftest import TEST_COOKIE

TWO = """output_dir: data
sources:
  - id: main
    platform: ghost
    base_url: https://www.example.com/
    cookie_env: NEWSLETTER_COOKIE
  - id: pragmatic-engineer
    name: The Pragmatic Engineer
    platform: ghost
    base_url: https://newsletter.example.org
    cookie_env: PRAGMATIC_ENGINEER_COOKIE
"""


def load(tmp_path, text, env=""):
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")
    (tmp_path / ".env").write_text(env, encoding="utf-8")
    return load_config(tmp_path / "config.yaml", tmp_path / ".env")


def one(**fields):
    entry = {
        "id": "main",
        "platform": "ghost",
        "base_url": "https://www.example.com",
        "cookie_env": "NEWSLETTER_COOKIE",
    }
    entry.update(fields)
    lines = "\n".join(f"    {k}: {v}" for k, v in entry.items() if v is not None)
    return "sources:\n  - " + lines.lstrip()


# spec: newsletter-sources / Source list (Two newsletters); One folder per newsletter
def test_two_newsletters_each_with_folder_and_cookie(tmp_path):
    cfg = load(
        tmp_path,
        TWO,
        f"NEWSLETTER_COOKIE={TEST_COOKIE}\nPRAGMATIC_ENGINEER_COOKIE=substack.sid=TESTSECRET123\n",
    )
    main, pe = cfg.sources
    assert (main.id, main.base_url, main.display_name) == ("main", "https://www.example.com", "main")
    assert (pe.id, pe.display_name) == ("pragmatic-engineer", "The Pragmatic Engineer")
    assert main.cookie.reveal() == TEST_COOKIE and pe.cookie.reveal() == "substack.sid=TESTSECRET123"
    assert pe.root == cfg.output_dir / "pragmatic-engineer"
    assert pe.post_path("ai-sre") == cfg.output_dir / "pragmatic-engineer" / "posts" / "ai-sre.json"
    assert pe.post_id("ai-sre") == "pragmatic-engineer/ai-sre"
    assert len(cfg.secrets) == 5  # two cookies, the Gemini key, two database passwords


def test_selecting_sources(tmp_path):
    cfg = load(tmp_path, TWO)
    assert [s.id for s in cfg.select(None)] == ["main", "pragmatic-engineer"]
    assert [s.id for s in cfg.select(["pragmatic-engineer", "main"])] == ["main", "pragmatic-engineer"]
    with pytest.raises(ConfigError, match="unknown source 'nope'; valid sources: main, pragmatic-engineer"):
        cfg.select(["nope"])
    with pytest.raises(ConfigError, match="several sources are configured"):
        cfg.only_source()


@pytest.mark.parametrize(
    "text, message",
    [
        ("sources: []\n", "sources is not set"),
        ("sources: main\n", "sources is not set"),
        ("sources:\n  - main\n", "sources entry 1 must be a mapping"),
        (one(colour="red"), "unknown setting.*sources entry 1: colour"),
        (one(cookie_env=None), "sources entry 1 needs cookie_env"),
        (one(id="Main"), "source 'Main': id must be lowercase"),
        (one(id="logs"), "id can't be one of logs, models, postgres, posts, raw"),
        (one(platform="wordpress"), "unknown platform 'wordpress'; use one of: ghost"),
        (one(base_url="ftp://x"), "base_url must start with http"),
        (one(cookie_env="my-cookie"), "cookie_env must be an environment variable name"),
        (one(name="''"), "name must be non-empty text"),
    ],
)
def test_invalid_sources_rejected(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load(tmp_path, text)


# spec: newsletter-sources / Source list (Duplicate id)
def test_duplicate_id_rejected(tmp_path):
    with pytest.raises(ConfigError, match="duplicate source id 'main'"):
        load(tmp_path, TWO.replace("id: pragmatic-engineer", "id: main"))


def test_same_site_twice_rejected(tmp_path):
    with pytest.raises(ConfigError, match="same base_url site"):
        load(tmp_path, TWO.replace("https://newsletter.example.org", "https://www.example.com/other"))


# spec: newsletter-sources / Source list (Old single-site config)
def test_old_single_site_config_shows_the_entry_to_paste(tmp_path):
    old = "base_url: https://www.example.com\nboilerplate_prefixes:\n  - Welcome aboard\n"
    with pytest.raises(ConfigError) as e:
        load(tmp_path, old)
    message = str(e.value)
    assert "old single-newsletter settings" in message
    assert (
        "    base_url: https://www.example.com\n    platform: ghost\n    cookie_env: NEWSLETTER_COOKIE"
        in message
    )
    assert "      - 'Welcome aboard'" in message and "archive migrate --source <id>" in message


def test_bad_cookie_names_its_variable(tmp_path):
    with pytest.raises(ConfigError, match="PRAGMATIC_ENGINEER_COOKIE contains a line break"):
        load(tmp_path, TWO, 'PRAGMATIC_ENGINEER_COOKIE="a=1\\nX: 2"\n')

import logging

from newsletter_archive.config import Secret
from newsletter_archive.runlog import RedactFilter, RunSummary, close_logging, log_post, setup_logging

from .conftest import TEST_COOKIE


def redact(text):
    return RedactFilter(Secret(TEST_COOKIE)).redact(text)


def test_redacts_exact_value_and_pairs():
    assert "TESTSECRET123" not in redact(f"value={TEST_COOKIE}")
    assert redact("only SIGSECRET456 here") == "only [REDACTED] here"


def test_redacts_cookie_lines_and_ghost_cookies_even_if_unknown():
    # a filter that knows a *different* cookie still scrubs Cookie lines and ghost cookies
    other = RedactFilter(Secret("session=zzzzzzzzzzzz"))
    out = other.redact("headers: Cookie: a=b; c=d\nSet-Cookie: ghost-members-ssr=TESTSECRET123; path=/")
    assert "a=b" not in out and "TESTSECRET123" not in out
    assert "Cookie: [REDACTED]" in out
    assert other.redact("x ghost-members-ssr.sig=SIGSECRET456 y") == "x ghost-members-ssr.sig=[REDACTED] y"


def test_filter_redacts_args_and_exceptions(cfg):
    path = setup_logging(cfg, verbose=True)
    log = logging.getLogger("newsletter_archive.test")
    log.info("cookie is %s", TEST_COOKIE)
    try:
        raise ValueError(f"bad {TEST_COOKIE}")
    except ValueError:
        log.exception("failed")
    close_logging()
    text = path.read_text()
    assert "TESTSECRET123" not in text and "SIGSECRET456" not in text
    assert "ValueError" in text and "[REDACTED]" in text


def test_log_file_location_and_unique_names(cfg):
    a = setup_logging(cfg)
    close_logging()
    b = setup_logging(cfg)
    close_logging()
    assert a.parent == cfg.logs_dir and a != b
    assert a.name.startswith("run-") and a.suffix == ".log"


def test_quiet_statuses_go_to_debug(cfg, capsys):
    path = setup_logging(cfg, verbose=False)
    log_post("a", "fetch", "skipped")
    log_post("b", "fetch", "failed", "HTTP 404")
    close_logging()
    out = capsys.readouterr().out
    assert "a  fetch  skipped" not in out and "b  fetch  failed  HTTP 404" in out
    assert "a  fetch  skipped" in path.read_text()


def test_empty_cookie_filter_is_harmless():
    assert RedactFilter(Secret("")).redact("plain text") == "plain text"


def test_summary_line():
    s = RunSummary(discovered=252, fetched=5, ok=4, partial=1, failed=0)
    assert s.line() == "Summary: discovered=252 fetched=5 ok=4 partial=1 failed=0"
    s.skipped, s.categories_incomplete = 3, True
    assert s.line().endswith("(skipped=3) [categories incomplete]")


def test_redacts_api_key_and_db_passwords():
    from .conftest import TEST_DB_PASSWORD, TEST_GEMINI_KEY

    f = RedactFilter(Secret(TEST_COOKIE), Secret(TEST_GEMINI_KEY), Secret(TEST_DB_PASSWORD), Secret(None))
    out = f.redact(f"key={TEST_GEMINI_KEY} pw={TEST_DB_PASSWORD}")
    assert TEST_GEMINI_KEY not in out and TEST_DB_PASSWORD not in out
    # short secrets are not used for redaction: they would garble ordinary words
    assert RedactFilter(Secret(None), Secret("archive")).redact("archive index") == "archive index"
    # any Google-style key is scrubbed, even one the filter wasn't given
    other = "AIza" + "B" * 35
    assert RedactFilter(Secret(None)).redact(f"x {other} y") == "x [REDACTED] y"


def test_server_log_is_daily_and_old_ones_are_removed(tmp_path):
    from datetime import UTC, datetime

    from newsletter_archive.runlog import server_log_file

    for day in ("20260901", "20261001", "20261102"):
        (tmp_path / f"mcp-{day}.log").write_text("x")
    (tmp_path / "mcp-notes.log").write_text("kept: not a dated log")
    (tmp_path / "run-20200101-000000Z.log").write_text("other logs are untouched")
    path = server_log_file(tmp_path, "mcp", datetime(2026, 11, 3, 9, 0, tzinfo=UTC))
    assert path.name == "mcp-20261103.log"
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "mcp-20261102.log",
        "mcp-notes.log",
        "run-20200101-000000Z.log",
    ]


def test_mcp_logging_goes_to_stderr_a_daily_file_and_captures_pool_warnings(cfg, capsys):
    import io
    import logging

    from newsletter_archive.runlog import close_logging, setup_logging

    err = io.StringIO()
    path = setup_logging(cfg, stream=err, prefix="mcp")
    try:
        assert path.name.startswith("mcp-") and len(path.stem) == len("mcp-YYYYMMDD")
        logging.getLogger("newsletter_archive.mcp").info("server line")
        logging.getLogger("psycopg.pool").warning(
            "error connecting in 'pool-1': password authentication failed"
        )
        logging.getLogger("psycopg.pool").info("routine pool chatter")
    finally:
        close_logging()
    assert capsys.readouterr().out == ""  # stdout untouched
    logged = path.read_text()
    assert "server line" in logged and "password authentication failed" in logged
    assert "routine pool chatter" not in logged and "server line" in err.getvalue()
    assert logging.getLogger("psycopg.pool").handlers == []


def test_every_cookie_is_redacted_pair_by_pair():
    # Two newsletters, two cookies: the second one's pairs and bare values are scrubbed too.
    second = "substack.sid=SECONDSECRET789; other=OTHERTOKEN012"
    f = RedactFilter(Secret(TEST_COOKIE), Secret(second))
    out = f.redact(f"saw other=OTHERTOKEN012 and just SECONDSECRET789 and {second}")
    assert "SECONDSECRET789" not in out and "OTHERTOKEN012" not in out
    assert out.count("[REDACTED]") >= 3

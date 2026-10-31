import logging

from scarlet_archive.config import Secret
from scarlet_archive.runlog import RedactFilter, RunSummary, close_logging, log_post, setup_logging

from .conftest import TEST_COOKIE


def redact(text):
    return RedactFilter(Secret(TEST_COOKIE)).redact(text)


def test_redacts_exact_value_and_pairs():
    assert "TESTSECRET123" not in redact(f"value={TEST_COOKIE}")
    assert redact("only SIGSECRET456 here") == "only [REDACTED] here"


def test_redacts_cookie_lines_and_ghost_cookies_even_if_unknown():
    out = redact("headers: Cookie: a=b; c=d\nSet-Cookie: ghost-members-ssr=OTHERVALUE; path=/")
    assert "a=b" not in out and "OTHERVALUE" not in out
    assert "Cookie: [REDACTED]" in out
    assert redact("x ghost-members-ssr.sig=abc123 y") == "x ghost-members-ssr.sig=[REDACTED] y"


def test_filter_redacts_args_and_exceptions(cfg):
    path = setup_logging(cfg, verbose=True)
    log = logging.getLogger("scarlet_archive.test")
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

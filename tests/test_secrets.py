"""spec: archive-cli / Verbose run — the cookie never shows up anywhere."""

from .cli_helpers import BASE, CliEnv
from .conftest import SECRET_PARTS, TEST_COOKIE
from .sitebuilder import ghost_site


def all_output_files(root):
    return [p for p in root.rglob("*") if p.is_file()]


def test_cookie_never_in_console_logs_or_data(tmp_path, monkeypatch):
    env = CliEnv(tmp_path, monkeypatch)
    ghost_site(env.transport, BASE, ["p1", "p2", "p3", "p4", "p5", "p6"], signed_out={"p6"})
    results = [env.run("run", "--limit", "5", "--verbose"), env.run("run", "--verbose")]
    assert results[0].exit_code == 0 and results[1].exit_code == 2
    # the cookie really was sent...
    assert any(c.headers.get("Cookie") == TEST_COOKIE for c in env.transport.calls)
    # ...but it appears nowhere we write or print
    for r in results:
        for part in SECRET_PARTS:
            assert part not in r.output
    files = all_output_files(env.data)
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for part in SECRET_PARTS:
            assert part not in text, f"{part} leaked into {path}"

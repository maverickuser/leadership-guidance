"""A fake Ghost site on 127.0.0.1, served from the structure-only fixtures."""

from __future__ import annotations

import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..sitebuilder import categories_page, post_page, sitemap_index, sitemap_urlset, tag_page


class FixtureSite:
    def __init__(self, slugs: list[str], categories: dict, cookie: str):
        self.slugs = list(slugs)
        self.categories = categories  # {slug: (Title, [pages of post slugs])}
        self.cookie = cookie
        self.flaky: dict[str, list[int]] = {}
        self.missing: set[str] = set()
        self.offsite: dict[str, str] = {}
        self.expire_after: int | None = None
        self.served_posts = 0
        self.requests: list[tuple[str, dict]] = []
        self._lock = threading.Lock()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def do_GET(self):
                with site._lock:
                    site.requests.append((self.path, dict(self.headers)))
                    status, body, headers = site.route(self.path, self.headers.get("Cookie"))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                data = body.encode("utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def post_requests(self) -> list[str]:
        return [p for p, _ in self.requests if p.strip("/") in self.slugs]

    def route(self, path: str, cookie: str | None):
        html = {"Content-Type": "text/html; charset=utf-8"}
        xml = {"Content-Type": "application/xml; charset=utf-8"}
        if path == "/sitemap.xml":
            return 200, sitemap_index(self.base, ["sitemap-pages.xml", "sitemap-posts.xml"]), xml
        if path == "/sitemap-posts.xml":
            return 200, sitemap_urlset(self.base, self.slugs), xml
        if path == "/categories/":
            return 200, categories_page(self.base, list(self.categories)), html
        m = re.fullmatch(r"/t/([^/]+)", path)
        if m:
            return 301, "", {"Location": f"/tag/{m.group(1)}/"}
        m = re.fullmatch(r"/tag/([^/]+)/(?:page/(\d+)/)?", path)
        if m and m.group(1) in self.categories:
            title, pages = self.categories[m.group(1)]
            n = int(m.group(2) or 1)
            if n > len(pages):
                return 404, "not found", html
            nxt = f"{self.base}/tag/{m.group(1)}/page/{n + 1}/" if n < len(pages) else None
            return 200, tag_page(title, pages[n - 1], nxt), html
        m = re.fullmatch(r"/([^/]+)/", path)
        if m and m.group(1) in self.slugs:
            slug = m.group(1)
            if slug in self.missing:
                return 404, "not found", html
            if self.flaky.get(slug):
                return self.flaky[slug].pop(0), "error", html
            if slug in self.offsite:
                return 302, "", {"Location": self.offsite[slug]}
            expired = self.expire_after is not None and self.served_posts >= self.expire_after
            signed_in = cookie == self.cookie and not expired
            if signed_in:
                self.served_posts += 1
            # Ghost refreshes its session cookie; the client must never store or resend it.
            headers = dict(html, **{"Set-Cookie": "ghost-members-ssr=SIGSECRET456; Path=/; HttpOnly"})
            return 200, post_page(slug, signed_in=signed_in), headers
        return 404, "not found", html

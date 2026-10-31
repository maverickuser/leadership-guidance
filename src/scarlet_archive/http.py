"""The only way the archive talks to the network.

One Client per run. It owns politeness (one request at a time, a minimum gap
between requests), retries, redirects and the Cookie header. It never logs
request or response headers.
"""

from __future__ import annotations

import http.cookiejar
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

from .config import BACKOFF_SECONDS, Config
from .errors import FetchError

log = logging.getLogger("scarlet_archive.http")

MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # a post page is well under 1 MB
REDIRECT_CODES = {301, 302, 303, 307, 308}
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


class TransportTimeout(Exception):
    pass


class TransportConnectionError(Exception):
    pass


class ResponseTooLarge(Exception):
    pass


@dataclass
class RawResponse:
    """One HTTP exchange, before redirects are followed."""

    status: int
    url: str
    text: str
    location: str | None = None


@dataclass
class Response:
    status: int
    url: str  # final URL after redirects
    text: str


class Transport(Protocol):
    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> RawResponse: ...


class RequestsTransport:
    """Real transport. Doesn't follow redirects and never stores cookies the site sends."""

    def __init__(self) -> None:
        self.session = requests.Session()
        # Refuse every Set-Cookie, so only the Cookie header we pass is ever sent.
        self.session.cookies.set_policy(http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
        self.session.trust_env = False  # ignore ~/.netrc credentials and proxy env vars

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> RawResponse:
        try:
            with self.session.get(
                url, headers=headers, timeout=timeout, allow_redirects=False, stream=True
            ) as r:
                body = self._read_capped(r)
                encoding = r.encoding if "charset" in r.headers.get("Content-Type", "").lower() else "utf-8"
                location = r.headers.get("Location")
                status = r.status_code
        except requests.Timeout as e:
            raise TransportTimeout(type(e).__name__) from None
        except requests.RequestException as e:
            raise TransportConnectionError(type(e).__name__) from None
        text = body.decode(encoding or "utf-8", errors="replace")
        return RawResponse(status=status, url=url, text=text, location=location)

    @staticmethod
    def _read_capped(r: requests.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        for chunk in r.iter_content(chunk_size=64 * 1024):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ResponseTooLarge(f"over {MAX_RESPONSE_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)


def origin(url: str) -> tuple[str, str, int]:
    """(scheme, host, port) with default ports filled in. Raises ValueError on a bad port."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    port = parts.port or (443 if scheme == "https" else 80)
    return scheme, (parts.hostname or "").lower(), port


def to_base_host(url: str, base_url: str) -> str:
    """The same path and query, on base_url's scheme and host (the cookie's only origin)."""
    parts, base = urlsplit(url), urlsplit(base_url)
    return urlunsplit((base.scheme, base.netloc, parts.path, parts.query, ""))


class Client:
    """The run's only HTTP client: one request at a time, retries, same-site redirects.

    The Cookie header is sent only to base_url's exact origin.
    """

    def __init__(
        self,
        config: Config,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._transport = transport or RequestsTransport()
        self._sleep = sleep
        self._clock = clock
        self._last_end: float | None = None
        self._base_origin = origin(config.base_url)
        self.request_count = 0  # HTTP exchanges, including redirect hops and retries

    # -- public -------------------------------------------------------------

    def get(self, url: str) -> Response:
        """GET a URL politely. Raises FetchError when it can't get a non-error response."""
        attempts = self.config.max_retries + 1
        status: int | None = None
        reason = ""
        for attempt in range(attempts):
            self._wait_for_slot()
            started = self._clock()
            try:
                resp = self._follow(url)
            except ResponseTooLarge as e:
                self._last_end = self._clock()
                raise FetchError(url, f"response too large ({e})") from None
            except FetchError:
                self._last_end = self._clock()
                raise
            except TransportTimeout:
                reason, status = "timeout", None
            except TransportConnectionError as e:
                reason, status = f"connection error ({e})", None
            else:
                self._last_end = self._clock()
                log.debug("GET %s -> %s (%d ms)", url, resp.status, (self._last_end - started) * 1000)
                if resp.status < 400:
                    return resp
                if resp.status < 500:
                    raise FetchError(url, f"HTTP {resp.status}", status=resp.status, attempts=attempt + 1)
                reason, status = f"HTTP {resp.status}", resp.status
            self._last_end = self._clock()
            if attempt == attempts - 1:
                break
            wait = BACKOFF_SECONDS[attempt]
            log.debug("GET %s failed: %s; retrying in %ss", url, reason, wait)
            self._sleep(wait)
        raise FetchError(url, f"{reason} after {attempts} attempts", status=status, attempts=attempts)

    # -- internals ----------------------------------------------------------

    def _wait_for_slot(self) -> None:
        if self._last_end is None:
            return
        remaining = self._last_end + self.config.delay_seconds - self._clock()
        if remaining > 0:
            self._sleep(remaining)

    def _headers_for(self, url: str) -> dict[str, str]:
        headers = {"User-Agent": self.config.user_agent, "Accept": ACCEPT}
        if self.config.cookie and origin(url) == self._base_origin:
            headers["Cookie"] = self.config.cookie.reveal()
        return headers

    def _follow(self, url: str) -> Response:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            self.request_count += 1
            raw = self._transport(current, self._headers_for(current), self.config.timeout_seconds)
            if raw.status in REDIRECT_CODES and raw.location:
                target = urljoin(current, raw.location)
                try:
                    same = origin(target) == self._base_origin
                except ValueError:
                    raise FetchError(url, "malformed redirect", status=raw.status) from None
                if not same:
                    raise FetchError(url, "redirect to another site refused", status=raw.status)
                current = target
                continue
            return Response(status=raw.status, url=current, text=raw.text)
        raise FetchError(url, "too many redirects")

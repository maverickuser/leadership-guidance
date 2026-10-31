"""Test doubles for the HTTP layer: a scripted transport and a fake clock."""

from __future__ import annotations

from dataclasses import dataclass, field

from newsletter_archive.http import RawResponse, TransportConnectionError, TransportTimeout


class FakeClock:
    """Time only moves when someone sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


@dataclass
class Call:
    url: str
    headers: dict
    at: float


@dataclass
class FakeTransport:
    """Routes map a URL to a list of results, served in order (the last one repeats).

    A result is an int status, a (status, text) tuple, ("redirect", location),
    a RawResponse, or one of the exception classes / instances.
    """

    routes: dict = field(default_factory=dict)
    clock: FakeClock | None = None
    request_seconds: float = 0.0
    calls: list[Call] = field(default_factory=list)

    def add(self, url: str, *results) -> "FakeTransport":
        self.routes.setdefault(url, []).extend(results)
        return self

    def __call__(self, url, headers, timeout):
        at = self.clock.now if self.clock else 0.0
        self.calls.append(Call(url, dict(headers), at))
        if self.clock and self.request_seconds:
            self.clock.now += self.request_seconds
        queue = self.routes.get(url)
        if not queue:
            return RawResponse(404, url, "not found")
        result = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(result, type) and issubclass(result, Exception):
            raise result("boom")
        if isinstance(result, Exception):
            raise result
        if isinstance(result, RawResponse):
            return result
        if isinstance(result, int):
            return RawResponse(result, url, "")
        if result[0] == "redirect":
            return RawResponse(301, url, "", location=result[1])
        return RawResponse(result[0], url, result[1])

    def urls(self) -> list[str]:
        return [c.url for c in self.calls]


__all__ = ["FakeClock", "FakeTransport", "TransportTimeout", "TransportConnectionError"]

"""Error types. Messages here are shown to the user, so they never include secrets."""

from __future__ import annotations

MISSING_COOKIE_MESSAGE = "SCARLETINK_COOKIE is not set. Copy your Cookie header into .env (see README)."
NOT_SIGNED_IN_MESSAGE = "Your cookie looks expired. Copy a fresh Cookie header into .env."


class ArchiveError(Exception):
    """Base class for all archive errors."""

    exit_code = 1


class ConfigError(ArchiveError):
    exit_code = 1


class MissingCookieError(ArchiveError):
    exit_code = 1

    def __init__(self) -> None:
        super().__init__(MISSING_COOKIE_MESSAGE)


class NotSignedInError(ArchiveError):
    exit_code = 2

    def __init__(self, slug: str) -> None:
        super().__init__(NOT_SIGNED_IN_MESSAGE)
        self.slug = slug


class FetchError(ArchiveError):
    """A single request failed. Not fatal for the run."""

    def __init__(self, url: str, reason: str, status: int | None = None, attempts: int = 1):
        super().__init__(f"{reason} ({url})")
        self.url = url
        self.reason = reason
        self.status = status
        self.attempts = attempts


class ExtractError(ArchiveError):
    """A single post could not be extracted or validated. Not fatal for the run."""

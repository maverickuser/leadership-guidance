"""Error types. Messages here are shown to the user, so they never include secrets."""

from __future__ import annotations

# Why one newsletter stopped, as `archive run` reports it and the weekly job shows it:
# "{newsletter}: {reason} ({cookie_env})."
MISSING_COOKIE_REASON = "cookie is not set. Copy your Cookie header into .env"
NOT_SIGNED_IN_REASON = "your cookie looks expired. Paste a fresh Cookie header into .env"


def stop_message(name: str, reason: str, cookie_env: str) -> str:
    """The user-facing line for a newsletter that stopped, naming its .env variable."""
    return f"{name}: {reason} ({cookie_env})."


class ArchiveError(Exception):
    """Base class for all archive errors."""

    exit_code = 1


class ConfigError(ArchiveError):
    exit_code = 1


class SourceStopError(ArchiveError):
    """One newsletter can't go on (its cookie is missing or expired). The run continues with
    the other newsletters and reports `Stopped: {name} ({cookie_env}): {reason}` at the end."""

    reason = ""

    def __init__(self, name: str, cookie_env: str) -> None:
        super().__init__(stop_message(name, self.reason, cookie_env))
        self.name = name
        self.cookie_env = cookie_env


class MissingCookieError(SourceStopError):
    exit_code = 1
    reason = MISSING_COOKIE_REASON


class NotSignedInError(SourceStopError):
    exit_code = 2
    reason = NOT_SIGNED_IN_REASON

    def __init__(self, slug: str, name: str, cookie_env: str) -> None:
        super().__init__(name, cookie_env)
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


class DatabaseError(ArchiveError):
    """The search database can't be used. Messages never include passwords or connection strings."""

    exit_code = 1


class DatabaseUnavailableError(DatabaseError):
    def __init__(self, host: str, port: int) -> None:
        super().__init__(
            f"Can't reach the search database at {host}:{port}. Start it: docker compose up -d --wait db"
        )


class DatabaseAuthError(DatabaseError):
    def __init__(self, user: str, env_name: str) -> None:
        super().__init__(f"The database rejected the password for {user}; check {env_name} in .env")


class MissingDbPasswordError(DatabaseError):
    def __init__(self, env_name: str) -> None:
        super().__init__(f"{env_name} is not set. Add it to .env (see .env.example).")


class SchemaVersionError(DatabaseError):
    """The database schema is older or newer than this code expects."""


class IndexBusyError(DatabaseError):
    def __init__(self) -> None:
        super().__init__("An index run is already active; wait for it to finish.")

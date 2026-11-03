"""Postgres access for search: connections per role, migrations, the index lock and generations.

Two roles share the database: ``archive_writer`` (the indexer; owns the schema) and
``archive_reader`` (the MCP server; read-only). Connection strings carry passwords, so they
are built here and never logged or put in error messages.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from typing import Literal

import psycopg
from psycopg.conninfo import make_conninfo

from .. import __version__
from ..config import DB_READER_PASSWORD_ENV, DB_WRITER_PASSWORD_ENV, Config, Secret
from ..errors import (
    DatabaseAuthError,
    DatabaseError,
    DatabaseUnavailableError,
    IndexBusyError,
    MissingDbPasswordError,
    SchemaVersionError,
)

log = logging.getLogger("newsletter_archive.search.db")

Role = Literal["writer", "reader"]
ROLE_USERS: dict[Role, str] = {"writer": "archive_writer", "reader": "archive_reader"}
ROLE_ENV: dict[Role, str] = {"writer": DB_WRITER_PASSWORD_ENV, "reader": DB_READER_PASSWORD_ENV}
CONNECT_TIMEOUT_SECONDS = 5

# Advisory lock keys (any fixed bigints; unique to this app).
INDEX_LOCK_KEY = 7_431_002_101
MIGRATION_LOCK_KEY = 7_431_002_102

MIGRATION_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")
# libpq messages meaning the server isn't there (as opposed to refusing us).
_UNREACHABLE_MARKERS = (
    "connection refused",
    "could not connect",
    "timeout expired",
    "could not translate host name",
    "no such file or directory",
    "network is unreachable",
    "server closed the connection",
)
GENERATION_STATUSES = ("building", "ready", "active", "retired")


def _password(config: Config, role: Role) -> Secret:
    return config.db_writer_password if role == "writer" else config.db_reader_password


def conninfo(config: Config, role: Role, dbname: str | None = None) -> str:
    """libpq connection string for a role. Contains the password: never log it."""
    password = _password(config, role)
    if not password:
        raise MissingDbPasswordError(ROLE_ENV[role])
    s = config.search
    return make_conninfo(
        host=s.db_host,
        port=s.db_port,
        dbname=dbname or s.db_name,
        user=ROLE_USERS[role],
        password=password.reveal(),
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        application_name=f"newsletter-archive/{__version__}",
    )


def _connect_error(config: Config, role: Role, error: psycopg.OperationalError) -> DatabaseError:
    """Turn a libpq connection error into one the user can act on, never quoting secrets."""
    message = str(error)
    lowered = message.lower()
    if "password authentication failed" in lowered:
        return DatabaseAuthError(ROLE_USERS[role], ROLE_ENV[role])
    if any(marker in lowered for marker in _UNREACHABLE_MARKERS) and "fatal:" not in lowered:
        return DatabaseUnavailableError(config.search.db_host, config.search.db_port)
    # The server answered but refused (unknown database, no CONNECT, too many clients, …).
    lines = [line.strip() for line in message.strip().splitlines() if line.strip()]
    reason = lines[-1] if lines else type(error).__name__
    for secret in (config.db_writer_password, config.db_reader_password):
        if secret:
            reason = reason.replace(secret.reveal(), "[REDACTED]")
    s = config.search
    return DatabaseError(f"The search database at {s.db_host}:{s.db_port} refused the connection: {reason}")


def connect(config: Config, role: Role, *, dbname: str | None = None) -> psycopg.Connection:
    """Open an autocommit connection as the writer or reader role.

    Autocommit: each statement commits on its own, and every ``with conn.transaction()`` block
    is a real transaction that commits when the block ends (never a savepoint inside an
    implicit transaction), so the session-level index lock never holds a transaction open.
    """
    info = conninfo(config, role, dbname)
    try:
        conn = psycopg.connect(info, autocommit=True)
    except psycopg.OperationalError as e:
        raise _connect_error(config, role, e) from None
    log.debug(
        "connected to %s:%s/%s as %s",
        config.search.db_host,
        config.search.db_port,
        dbname or config.search.db_name,
        ROLE_USERS[role],
    )
    return conn


# --- migrations -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations() -> list[Migration]:
    """The packaged SQL migrations, in order. Versions must run 1, 2, 3… with no gaps."""
    found: list[Migration] = []
    for entry in resources.files("newsletter_archive.search.migrations").iterdir():
        m = MIGRATION_RE.match(entry.name)
        if m:
            found.append(Migration(int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort(key=lambda mig: mig.version)
    check_migration_sequence(found)
    return found


def check_migration_sequence(migrations: list[Migration]) -> None:
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise SchemaVersionError(f"migration versions must be 1..N with no gaps, got {versions}")


def latest_version() -> int:
    return len(load_migrations())


def current_version(conn: psycopg.Connection) -> int:
    """Highest applied migration, or 0 for an empty database."""
    row = conn.execute("SELECT to_regclass('public.schema_version') IS NOT NULL").fetchone()
    if not row or not row[0]:
        return 0
    row = conn.execute("SELECT coalesce(max(version), 0) FROM public.schema_version").fetchone()
    return int(row[0]) if row else 0


def apply_migrations(conn: psycopg.Connection, migrations: list[Migration] | None = None) -> list[int]:
    """Apply pending migrations in one transaction. Returns the versions applied.

    Raises:
        SchemaVersionError: the database is newer than this code.
    """
    migrations = load_migrations() if migrations is None else migrations
    check_migration_sequence(migrations)
    applied: list[int] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,))
        current = current_version(conn)
        if current > len(migrations):
            raise SchemaVersionError(
                f"the search database is at schema version {current}, newer than this code "
                f"({len(migrations)}); update the code"
            )
        for mig in migrations[current:]:
            conn.execute(mig.sql.encode("utf-8"))
            conn.execute("INSERT INTO public.schema_version (version) VALUES (%s)", (mig.version,))
            applied.append(mig.version)
            log.info("applied database migration %s", mig.name)
    return applied


def check_schema(conn: psycopg.Connection) -> None:
    """For read-only users: the schema must match this code exactly."""
    current, latest = current_version(conn), latest_version()
    if current < latest:
        raise SchemaVersionError(
            f"the search database is at schema version {current}; run `archive index` to upgrade it"
        )
    if current > latest:
        raise SchemaVersionError(
            f"the search database is at schema version {current}, newer than this code ({latest})"
        )


# --- index lock -------------------------------------------------------------------------------


@contextmanager
def index_lock(conn: psycopg.Connection) -> Iterator[None]:
    """Hold the database-wide index lock for the life of the block.

    Raises:
        IndexBusyError: another index run holds it.
    """
    row = conn.execute("SELECT pg_try_advisory_lock(%s)", (INDEX_LOCK_KEY,)).fetchone()
    if not row or not row[0]:
        raise IndexBusyError()
    try:
        yield
    finally:
        _release_index_lock(conn)


def _release_index_lock(conn: psycopg.Connection) -> None:
    """Unlock without hiding an error from the body. If the unlock can't run, the lock goes
    away with the session, so close the connection rather than keep a locked session alive."""
    if conn.closed:
        return
    try:
        if conn.info.transaction_status == psycopg.pq.TransactionStatus.INERROR:
            conn.rollback()
        conn.execute("SELECT pg_advisory_unlock(%s)", (INDEX_LOCK_KEY,))
    except psycopg.Error:
        log.warning("could not release the index lock cleanly; closing the connection to drop it")
        conn.close()


# --- generations ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Generation:
    generation: int
    status: str
    embedding_model: str
    embedding_dim: int

    @property
    def schema(self) -> str:
        """Schema name; built from a validated integer, never from user input."""
        if not isinstance(self.generation, int) or self.generation < 1:
            raise ValueError("generation must be a positive integer")
        return f"idx_g{self.generation}"


_GEN_COLUMNS = "generation, status, embedding_model, embedding_dim"


def _gen(row: tuple[object, ...]) -> Generation:
    return Generation(int(str(row[0])), str(row[1]), str(row[2]), int(str(row[3])))


def list_generations(conn: psycopg.Connection) -> list[Generation]:
    rows = conn.execute(f"SELECT {_GEN_COLUMNS} FROM public.generations ORDER BY generation").fetchall()
    return [_gen(r) for r in rows]


def active_generation(conn: psycopg.Connection) -> Generation | None:
    row = conn.execute(f"SELECT {_GEN_COLUMNS} FROM public.generations WHERE status = 'active'").fetchone()
    return _gen(row) if row else None


def create_generation(conn: psycopg.Connection, model: str, dim: int) -> Generation:
    """Create the next generation (status ``building``) with its schema, tables and indexes."""
    with conn.transaction():
        row = conn.execute("SELECT coalesce(max(generation), 0) + 1 FROM public.generations").fetchone()
        n = int(row[0]) if row else 1
        conn.execute(
            "INSERT INTO public.generations (generation, status, embedding_model, embedding_dim) "
            "VALUES (%s, 'building', %s, %s)",
            (n, model, dim),
        )
        conn.execute("SELECT public.create_generation(%s, %s)", (n, dim))
    log.info("created index generation %s (%s, %s dimensions)", n, model, dim)
    return Generation(n, "building", model, dim)


def set_generation_status(conn: psycopg.Connection, generation: int, status: str) -> None:
    """Set a non-active status on a generation that isn't active.

    Use activate_generation to make one active; the active one only leaves that state when
    another is activated, so searches always have a generation to read.
    """
    if status not in GENERATION_STATUSES or status == "active":
        raise ValueError(f"invalid status for set_generation_status: {status!r}")
    with conn.transaction():
        row = conn.execute(
            "SELECT status FROM public.generations WHERE generation = %s FOR UPDATE", (generation,)
        ).fetchone()
        if not row:
            raise ValueError(f"no generation {generation}")
        if row[0] == "active":
            raise ValueError(f"generation {generation} is active; activate another one first")
        conn.execute("UPDATE public.generations SET status = %s WHERE generation = %s", (status, generation))


ACTIVATABLE = ("ready", "retired")  # retired: rollback to the previous generation


def activate_generation(conn: psycopg.Connection, generation: int) -> None:
    """Atomically make a ``ready`` (or, for rollback, ``retired``) generation active and retire
    the previous active one. A generation still ``building`` can never serve searches."""
    with conn.transaction():
        row = conn.execute(
            "SELECT status FROM public.generations WHERE generation = %s FOR UPDATE", (generation,)
        ).fetchone()
        if not row:
            raise ValueError(f"no generation {generation}")
        if row[0] == "active":
            return
        if row[0] not in ACTIVATABLE:
            raise ValueError(
                f"generation {generation} is {row[0]}; only a ready or retired one can be activated"
            )
        conn.execute(
            "UPDATE public.generations SET status = 'retired' WHERE status = 'active' AND generation <> %s",
            (generation,),
        )
        conn.execute(
            "UPDATE public.generations SET status = 'active', activated_at = now() WHERE generation = %s",
            (generation,),
        )

"""Fixtures for tests that need the search database.

Each test gets a fresh database (created from template1, which has pgvector and pg_search),
owned by archive_writer, and dropped afterwards. Passwords come from the environment, or
from the repo's .env when running locally.

When the database isn't running, `db` tests are skipped locally. CI sets
ARCHIVE_REQUIRE_DB=1 so a missing database fails the run instead.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest
from dotenv import dotenv_values
from psycopg.conninfo import make_conninfo

from newsletter_archive.config import Config, Secret

from ..conftest import make_config

REPO = Path(__file__).resolve().parents[2]
ENV_NAMES = ("ARCHIVE_DB_ADMIN_PASSWORD", "ARCHIVE_DB_WRITER_PASSWORD", "ARCHIVE_DB_READER_PASSWORD")


def _db_env() -> dict[str, str]:
    file_values = dotenv_values(REPO / ".env") if (REPO / ".env").is_file() else {}
    out = {}
    for name in ENV_NAMES:
        value = os.environ.get(name) or file_values.get(name)
        if value:
            out[name] = value
    return out


def _unavailable(reason: str) -> None:
    if os.environ.get("ARCHIVE_REQUIRE_DB") == "1":
        pytest.fail(f"search database required but unavailable: {reason}")
    pytest.skip(f"search database not available ({reason}); run: docker compose up -d --wait db")


class PgAdmin:
    """Superuser access on 127.0.0.1, used only to create and drop test databases."""

    def __init__(self, port: int, env: dict[str, str]) -> None:
        self.port = port
        self.env = env

    def info(self, dbname: str = "postgres") -> str:
        return make_conninfo(
            host="127.0.0.1",
            port=self.port,
            dbname=dbname,
            user="postgres",
            password=self.env["ARCHIVE_DB_ADMIN_PASSWORD"],
            connect_timeout=3,
        )

    def execute(self, statement: str, dbname: str = "postgres") -> None:
        with psycopg.connect(self.info(dbname), autocommit=True) as conn:
            conn.execute(statement)


@pytest.fixture(scope="session")
def pg_admin() -> PgAdmin:
    env = _db_env()
    missing = [n for n in ENV_NAMES if n not in env]
    if missing:
        _unavailable(f"missing {', '.join(missing)}")
    admin = PgAdmin(int(os.environ.get("ARCHIVE_DB_PORT", "5433")), env)
    try:
        admin.execute("SELECT 1")
    except psycopg.OperationalError as e:
        _unavailable(type(e).__name__)
    return admin


@pytest.fixture
def db_config(tmp_path, pg_admin: PgAdmin) -> Iterator[Config]:
    """A Config pointing at a fresh, empty test database."""
    name = f"archive_test_{uuid.uuid4().hex[:12]}"
    pg_admin.execute(f'CREATE DATABASE "{name}" OWNER archive_writer')
    pg_admin.execute(f'GRANT CONNECT ON DATABASE "{name}" TO archive_reader')
    cfg = make_config(tmp_path)
    cfg = replace(
        cfg,
        search=replace(cfg.search, db_port=pg_admin.port, db_name=name),
        db_writer_password=Secret(pg_admin.env["ARCHIVE_DB_WRITER_PASSWORD"]),
        db_reader_password=Secret(pg_admin.env["ARCHIVE_DB_READER_PASSWORD"]),
    )
    try:
        yield cfg
    finally:
        pg_admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

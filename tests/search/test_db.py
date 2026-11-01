"""Database helpers that don't need a running database."""

from __future__ import annotations

from dataclasses import replace

import psycopg
import pytest

from newsletter_archive.config import Secret
from newsletter_archive.errors import (
    DatabaseAuthError,
    DatabaseError,
    DatabaseUnavailableError,
    MissingDbPasswordError,
    SchemaVersionError,
)
from newsletter_archive.search import db

from ..conftest import TEST_DB_PASSWORD


@pytest.fixture
def wcfg(cfg):
    return replace(cfg, db_writer_password=Secret(TEST_DB_PASSWORD), db_reader_password=Secret("reader-pw"))


def test_conninfo_per_role(wcfg):
    info = db.conninfo(wcfg, "writer")
    assert "user=archive_writer" in info and "port=5433" in info and "dbname=archive" in info
    assert "host=127.0.0.1" in info and "connect_timeout=5" in info
    assert "user=archive_reader" in db.conninfo(wcfg, "reader", dbname="other")
    assert "dbname=other" in db.conninfo(wcfg, "reader", dbname="other")


@pytest.mark.parametrize(("role", "env"), [("writer", "WRITER"), ("reader", "READER")])
def test_missing_password_names_the_env_variable(cfg, role, env):
    with pytest.raises(MissingDbPasswordError, match=f"ARCHIVE_DB_{env}_PASSWORD is not set"):
        db.conninfo(cfg, role)


def test_connect_maps_auth_failure_without_leaking(wcfg, monkeypatch):
    def fail(info, **kwargs):
        raise psycopg.OperationalError(f'password authentication failed for user "archive_writer" {info}')

    monkeypatch.setattr(db.psycopg, "connect", fail)
    with pytest.raises(DatabaseAuthError) as exc:
        db.connect(wcfg, "writer")
    assert "ARCHIVE_DB_WRITER_PASSWORD" in str(exc.value)
    assert TEST_DB_PASSWORD not in str(exc.value) and exc.value.__cause__ is None


def test_connect_maps_unreachable_server(wcfg, monkeypatch):
    def fail(info, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(db.psycopg, "connect", fail)
    with pytest.raises(DatabaseUnavailableError, match=r"127\.0\.0\.1:5433.*docker compose up"):
        db.connect(wcfg, "reader")


def test_connect_reports_server_refusals_with_the_reason(wcfg, monkeypatch):
    def fail(info, **kwargs):
        raise psycopg.OperationalError(
            'connection to server at "127.0.0.1", port 5433 failed: '
            f'FATAL:  database "archve" does not exist {TEST_DB_PASSWORD}'
        )

    monkeypatch.setattr(db.psycopg, "connect", fail)
    with pytest.raises(DatabaseError) as exc:
        db.connect(wcfg, "writer")
    msg = str(exc.value)
    assert not isinstance(exc.value, DatabaseUnavailableError)
    assert 'database "archve" does not exist' in msg and "refused the connection" in msg
    assert TEST_DB_PASSWORD not in msg


def test_connect_refusal_without_message(wcfg, monkeypatch):
    def fail(info, **kwargs):
        raise psycopg.OperationalError("")

    monkeypatch.setattr(db.psycopg, "connect", fail)
    with pytest.raises(DatabaseError, match="OperationalError"):
        db.connect(wcfg, "writer")


def test_packaged_migrations_are_numbered_from_one():
    migrations = db.load_migrations()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[0].name == "001_init.sql" and "create_generation" in migrations[0].sql
    assert db.latest_version() == len(migrations)


def test_migration_gaps_rejected():
    gap = [db.Migration(1, "001_a.sql", ""), db.Migration(3, "003_c.sql", "")]
    with pytest.raises(SchemaVersionError, match="no gaps"):
        db.check_migration_sequence(gap)


def test_generation_schema_name_is_validated():
    assert db.Generation(3, "active", "m", 4).schema == "idx_g3"
    with pytest.raises(ValueError, match="positive integer"):
        _ = db.Generation(0, "active", "m", 4).schema


def test_set_generation_status_rejects_active_and_unknown():
    for status in ("active", "gone"):
        with pytest.raises(ValueError, match="invalid status"):
            db.set_generation_status(None, 1, status)  # type: ignore[arg-type]

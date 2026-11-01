"""Migrations, roles, generations and the index lock against a real ParadeDB database."""

from __future__ import annotations

from dataclasses import replace

import psycopg
import pytest

from newsletter_archive.config import Secret
from newsletter_archive.errors import DatabaseAuthError, DatabaseError, IndexBusyError, SchemaVersionError
from newsletter_archive.search import db

pytestmark = pytest.mark.db


@pytest.fixture
def writer(db_config):
    with db.connect(db_config, "writer") as conn:
        db.apply_migrations(conn)
        conn.commit()
        yield conn


def test_migrations_apply_once(db_config):
    with db.connect(db_config, "writer") as conn:
        assert db.current_version(conn) == 0
        assert db.apply_migrations(conn) == [1]
        assert db.apply_migrations(conn) == []
        assert db.current_version(conn) == db.latest_version()
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            )
        }
    assert {"schema_version", "generations", "embedding_cache", "summary_cache"} <= tables
    assert {"index_runs", "index_failures"} <= tables


def test_reader_needs_migrated_schema(db_config, writer):
    with db.connect(db_config, "reader") as reader:
        db.check_schema(reader)
        assert db.list_generations(reader) == []
    writer.execute("INSERT INTO schema_version (version) VALUES (99)")
    writer.commit()
    with pytest.raises(SchemaVersionError, match="newer than this code"):
        db.apply_migrations(writer)
    writer.rollback()
    with db.connect(db_config, "reader") as reader, pytest.raises(SchemaVersionError, match="newer"):
        db.check_schema(reader)


def test_empty_database_needs_upgrade(db_config):
    with db.connect(db_config, "reader") as reader, pytest.raises(SchemaVersionError, match="archive index"):
        db.check_schema(reader)


def test_unknown_database_is_not_reported_as_down(db_config):
    missing = replace(db_config, search=replace(db_config.search, db_name="archive_missing_db"))
    with pytest.raises(DatabaseError, match="does not exist"):
        db.connect(missing, "writer")


def test_reader_sees_only_what_migrations_grant(db_config, writer):
    with db.connect(db_config, "reader") as reader:
        for table in ("embedding_cache", "summary_cache", "index_runs", "index_failures"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                reader.execute(f"SELECT 1 FROM {table}")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute("CREATE TABLE sneaky (x int)")


def test_wrong_password_is_reported(db_config):
    bad = replace(db_config, db_reader_password=Secret("wrong-password"))
    with pytest.raises(DatabaseAuthError, match="ARCHIVE_DB_READER_PASSWORD"):
        db.connect(bad, "reader")


def _insert_passage(conn, schema, pid, title, body, *, kind="passage", n=0, dim=4):
    conn.execute(
        f"INSERT INTO {schema}.posts (post_id, title, url, word_count, status, fingerprint) "
        "VALUES (%s, %s, 'https://example.com/p', 10, 'ok', 'fp') ON CONFLICT DO NOTHING",
        (pid.split("#")[0], title),
    )
    conn.execute(
        f"INSERT INTO {schema}.passages (passage_id, post_id, kind, n, body, title, search_title, "
        "search_body, word_count, embed_sha256, embedding) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,10,'h',%s)",
        (pid, pid.split("#")[0], kind, n, body, title, title, body, "[" + ",".join(["0.5"] * dim) + "]"),
    )


def test_generation_tables_bm25_and_grants(db_config, writer):
    gen = db.create_generation(writer, "test-model", 4)
    assert (gen.generation, gen.status, gen.schema) == (1, "building", "idx_g1")
    _insert_passage(writer, gen.schema, "post-a#0", "Delegation", "Delegating work builds trust.")
    _insert_passage(writer, gen.schema, "post-a#summary", "Delegation", "A summary.", kind="summary", n=-1)
    writer.commit()

    hits = writer.execute(
        f"SELECT passage_id FROM {gen.schema}.passages WHERE search_body ||| 'delegate'"
    ).fetchall()
    assert hits == [("post-a#0",)]

    with pytest.raises(psycopg.errors.DataException):
        _insert_passage(writer, gen.schema, "post-b#0", "T", "body", dim=3)

    with db.connect(db_config, "reader") as reader:
        assert reader.execute(f"SELECT count(*) FROM {gen.schema}.passages").fetchone() == (2,)
        assert db.list_generations(reader)[0].embedding_model == "test-model"
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute(f"DELETE FROM {gen.schema}.passages")


def test_create_generation_rejects_bad_dimension(writer):
    with pytest.raises(psycopg.errors.RaiseException, match="dimension"):
        writer.execute("SELECT create_generation(5, 0)")
    writer.rollback()
    with pytest.raises(psycopg.errors.RaiseException, match="positive integer"):
        writer.execute("SELECT create_generation(0, 4)")


def test_generation_lifecycle(writer):
    first = db.create_generation(writer, "m", 4)
    second = db.create_generation(writer, "m", 4)
    assert db.active_generation(writer) is None
    with pytest.raises(ValueError, match="is building"):
        db.activate_generation(writer, first.generation)
    db.set_generation_status(writer, first.generation, "ready")
    db.activate_generation(writer, first.generation)
    db.activate_generation(writer, first.generation)  # already active: no-op
    assert db.active_generation(writer).generation == 1
    with pytest.raises(ValueError, match="is active"):
        db.set_generation_status(writer, first.generation, "retired")
    db.set_generation_status(writer, second.generation, "ready")
    db.activate_generation(writer, second.generation)
    assert {g.generation: g.status for g in db.list_generations(writer)} == {1: "retired", 2: "active"}
    db.activate_generation(writer, first.generation)  # rollback to the retired one
    assert {g.generation: g.status for g in db.list_generations(writer)} == {1: "active", 2: "retired"}
    with pytest.raises(ValueError, match="no generation 9"):
        db.activate_generation(writer, 9)
    with pytest.raises(ValueError, match="no generation 9"):
        db.set_generation_status(writer, 9, "ready")


def test_work_inside_the_lock_is_committed(db_config):
    with db.connect(db_config, "writer") as conn:
        db.apply_migrations(conn)
        with db.index_lock(conn):
            db.create_generation(conn, "m", 4)
        conn.close()  # no explicit commit
    with db.connect(db_config, "reader") as reader:
        assert [g.generation for g in db.list_generations(reader)] == [1]


def test_lock_released_after_a_failed_transaction(db_config, writer):
    with pytest.raises(psycopg.errors.UndefinedTable), db.index_lock(writer):
        writer.execute("BEGIN")
        writer.execute("SELECT * FROM no_such_table")
    with db.connect(db_config, "writer") as other, db.index_lock(other):
        pass


def test_lock_dropped_when_the_session_dies(db_config, writer, pg_admin):
    pid = writer.info.backend_pid
    with pytest.raises(RuntimeError, match="boom"), db.index_lock(writer):
        pg_admin.execute(f"SELECT pg_terminate_backend({pid})", dbname=db_config.search.db_name)
        raise RuntimeError("boom")
    assert writer.closed
    with db.connect(db_config, "writer") as other, db.index_lock(other):
        pass


def test_lock_release_skips_a_closed_connection(db_config):
    conn = db.connect(db_config, "writer")
    with db.index_lock(conn):
        conn.close()
    with db.connect(db_config, "writer") as other, db.index_lock(other):
        pass


def test_index_lock_is_exclusive(db_config, writer):
    with db.connect(db_config, "writer") as other:
        with (
            db.index_lock(writer),
            pytest.raises(IndexBusyError, match="already active"),
            db.index_lock(other),
        ):
            pass
        with db.index_lock(other):
            pass

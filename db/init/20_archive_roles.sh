#!/bin/bash
# Runs once, when the database folder is first created (after ParadeDB's own bootstrap).
# archive_writer owns the archive database (the indexer); archive_reader can only read (the MCP server).
set -Eeuo pipefail

: "${ARCHIVE_DB_WRITER_PASSWORD:?missing}"
: "${ARCHIVE_DB_READER_PASSWORD:?missing}"

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres \
  -v writer_pw="$ARCHIVE_DB_WRITER_PASSWORD" -v reader_pw="$ARCHIVE_DB_READER_PASSWORD" <<-'EOSQL'
	CREATE ROLE archive_writer LOGIN PASSWORD :'writer_pw';
	CREATE ROLE archive_reader LOGIN PASSWORD :'reader_pw';
	ALTER DATABASE archive OWNER TO archive_writer;
	REVOKE ALL ON DATABASE archive FROM PUBLIC;
	GRANT CONNECT ON DATABASE archive TO archive_writer, archive_reader;
EOSQL

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d archive <<-'EOSQL'
	REVOKE CREATE ON SCHEMA public FROM PUBLIC;
	GRANT USAGE ON SCHEMA public TO archive_reader;
	ALTER DEFAULT PRIVILEGES FOR ROLE archive_writer GRANT SELECT ON TABLES TO archive_reader;
EOSQL

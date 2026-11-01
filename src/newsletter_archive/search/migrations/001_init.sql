-- 001: shared tables and the function that creates one index generation (schema idx_g{n}).
-- Runs as archive_writer (database owner). Extensions come from the image's template1.

CREATE TABLE schema_version (
  version    int PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

-- The "alias": exactly one generation is active and serves searches.
CREATE TABLE generations (
  generation      int PRIMARY KEY CHECK (generation > 0),
  status          text NOT NULL CHECK (status IN ('building', 'ready', 'active', 'retired')),
  embedding_model text NOT NULL,
  embedding_dim   int  NOT NULL CHECK (embedding_dim BETWEEN 1 AND 4000),
  created_at      timestamptz NOT NULL DEFAULT now(),
  activated_at    timestamptz
);
CREATE UNIQUE INDEX generations_one_active ON generations ((status)) WHERE status = 'active';

-- Content-addressed embeddings at full precision; survives generation rebuilds.
CREATE TABLE embedding_cache (
  embed_sha256 text NOT NULL,
  model        text NOT NULL,
  dim          int  NOT NULL,
  embedding    vector NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (embed_sha256, model, dim)
);

-- One summary per post text, model and prompt version; survives generation rebuilds.
CREATE TABLE summary_cache (
  summary_sha256 text PRIMARY KEY,
  model          text NOT NULL,
  prompt_version int  NOT NULL,
  summary        text NOT NULL,
  input_tokens   int,
  output_tokens  int,
  created_at     timestamptz NOT NULL DEFAULT now(),
  last_used_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE index_runs (
  run_id      bigserial PRIMARY KEY,
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  mode        text NOT NULL CHECK (mode IN ('incremental', 'build', 'swap', 'rollback', 'dry-run')),
  generation  int,
  counts      jsonb,
  status      text NOT NULL DEFAULT 'running'
              CHECK (status IN ('running', 'ok', 'partial', 'failed', 'aborted')),
  error       text
);

-- One row per post and failing stage; a summary failure does not mark the passages failed.
CREATE TABLE index_failures (
  post_id      text NOT NULL,
  stage        text NOT NULL CHECK (stage IN ('load', 'clean', 'chunk', 'summarize', 'embed', 'write')),
  error        text NOT NULL,
  fingerprint  text NOT NULL,
  attempts     int  NOT NULL DEFAULT 1,
  first_failed timestamptz NOT NULL DEFAULT now(),
  last_attempt timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (post_id, stage)
);

GRANT SELECT ON schema_version, generations TO archive_reader;

-- Creates schema idx_g{n} with identical tables, indexes and grants for every generation.
-- search_* columns hold accent-folded text (folded in Python): pg_search stems before it
-- folds accents, so "résumé" and "resume" would otherwise index differently.
CREATE FUNCTION create_generation(n int, dim int) RETURNS text
LANGUAGE plpgsql AS $fn$
DECLARE
  s text := format('idx_g%s', n);
BEGIN
  IF n IS NULL OR n < 1 THEN
    RAISE EXCEPTION 'generation must be a positive integer';
  END IF;
  IF dim IS NULL OR dim < 1 OR dim > 4000 THEN
    RAISE EXCEPTION 'dimension must be between 1 and 4000';
  END IF;

  EXECUTE format('CREATE SCHEMA %I', s);

  EXECUTE format($sql$
    CREATE TABLE %I.posts (
      post_id        text PRIMARY KEY,
      title          text NOT NULL,
      subtitle       text,
      url            text NOT NULL,
      author         text,
      published_date date,
      categories     text[] NOT NULL DEFAULT '{}',
      word_count     int  NOT NULL,
      status         text NOT NULL CHECK (status IN ('ok', 'partial')),
      fingerprint    text NOT NULL,
      summary_sha256 text,
      fetched_at     timestamptz,
      indexed_at     timestamptz NOT NULL DEFAULT now()
    )$sql$, s);

  EXECUTE format($sql$
    CREATE TABLE %I.passages (
      passage_id     text PRIMARY KEY,
      post_id        text NOT NULL REFERENCES %I.posts ON DELETE CASCADE,
      kind           text NOT NULL DEFAULT 'passage' CHECK (kind IN ('passage', 'summary')),
      n              int  NOT NULL,
      heading        text,
      body           text NOT NULL,
      title          text NOT NULL,
      search_title   text NOT NULL,
      search_heading text,
      search_body    text NOT NULL,
      published_date date,
      categories     text[] NOT NULL DEFAULT '{}',
      word_count     int  NOT NULL,
      embed_sha256   text NOT NULL,
      embedding      halfvec(%s) NOT NULL,
      UNIQUE (post_id, n)
    )$sql$, s, s, dim);

  EXECUTE format('CREATE INDEX ON %I.passages (published_date)', s);
  EXECUTE format('CREATE INDEX ON %I.passages USING gin (categories)', s);
  EXECUTE format('CREATE INDEX ON %I.passages (kind)', s);
  EXECUTE format($sql$
    CREATE INDEX passages_bm25 ON %I.passages USING bm25 (
      passage_id,
      (search_title::pdb.simple('stemmer=english', 'ascii_folding=true')),
      (search_heading::pdb.simple('stemmer=english', 'ascii_folding=true')),
      (search_body::pdb.simple('stemmer=english', 'ascii_folding=true')),
      kind, categories, published_date
    )$sql$, s);

  EXECUTE format($sql$
    CREATE TABLE %I.boilerplate (
      cluster_id  int  NOT NULL,
      para_sha256 text PRIMARY KEY,
      post_count  int  NOT NULL,
      sample      text NOT NULL
    )$sql$, s);

  EXECUTE format('GRANT USAGE ON SCHEMA %I TO archive_reader', s);
  EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO archive_reader', s);
  RETURN s;
END
$fn$;

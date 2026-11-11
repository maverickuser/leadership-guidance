-- 002: every post and passage says which newsletter it came from (`source`).
--
-- New generations get `source text NOT NULL`. Existing generations get the column too, filled
-- from the `<source>/` prefix of each post id, so weekly updates and rollback keep working on
-- them. Rows from before `archive migrate` (slug-only ids) keep source NULL; the first index
-- after the move replaces them. The BM25 index lists `source` as a literal (untokenized)
-- field, so `source = ANY(...)` is an exact match that pg_search can apply inside the search
-- (checked against paradedb 0.26.2).

CREATE OR REPLACE FUNCTION create_generation(n int, dim int) RETURNS text
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
      source         text NOT NULL,
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
      source         text NOT NULL,
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
  EXECUTE format('CREATE INDEX ON %I.passages (source)', s);
  EXECUTE format($sql$
    CREATE INDEX passages_bm25 ON %I.passages USING bm25 (
      passage_id,
      (search_title::pdb.simple('stemmer=english', 'ascii_folding=true')),
      (search_heading::pdb.simple('stemmer=english', 'ascii_folding=true')),
      (search_body::pdb.simple('stemmer=english', 'ascii_folding=true')),
      kind, categories, published_date, (source::pdb.literal)
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

-- Existing generations: add and fill `source`, and rebuild the BM25 index with it.
DO $do$
DECLARE
  s text;
BEGIN
  FOR s IN
    SELECT format('idx_g%s', g.generation) FROM generations g
    WHERE EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = format('idx_g%s', g.generation))
    ORDER BY g.generation
  LOOP
    -- Drop the BM25 index first, so the fill below doesn't update it row by row.
    EXECUTE format('DROP INDEX IF EXISTS %I.passages_bm25', s);
    EXECUTE format('ALTER TABLE %I.posts ADD COLUMN IF NOT EXISTS source text', s);
    EXECUTE format('ALTER TABLE %I.passages ADD COLUMN IF NOT EXISTS source text', s);
    EXECUTE format(
      $sql$UPDATE %I.posts SET source = split_part(post_id, '/', 1) WHERE position('/' in post_id) > 0$sql$, s);
    EXECUTE format(
      $sql$UPDATE %I.passages SET source = split_part(post_id, '/', 1) WHERE position('/' in post_id) > 0$sql$, s);
    EXECUTE format('CREATE INDEX IF NOT EXISTS passages_source_idx ON %I.passages (source)', s);
    EXECUTE format($sql$
      CREATE INDEX passages_bm25 ON %I.passages USING bm25 (
        passage_id,
        (search_title::pdb.simple('stemmer=english', 'ascii_folding=true')),
        (search_heading::pdb.simple('stemmer=english', 'ascii_folding=true')),
        (search_body::pdb.simple('stemmer=english', 'ascii_folding=true')),
        kind, categories, published_date, (source::pdb.literal)
      )$sql$, s);
  END LOOP;
END
$do$;

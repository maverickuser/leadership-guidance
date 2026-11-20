-- 003: the weights behind each local embedding model name (openspec use-local-embeddings, lld §4).
--
-- An index version records only the model's name. If different weights were later put under the
-- same name, the stored vectors and new query vectors would silently disagree. The first index
-- run with a local model records the sha256 of its weights here; later runs and the MCP server
-- refuse weights whose hash differs.

CREATE TABLE IF NOT EXISTS embedding_models (
  name            text PRIMARY KEY,
  weights_sha256  text NOT NULL CHECK (weights_sha256 ~ '^[0-9a-f]{64}$'),
  created_at      timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT ON embedding_models TO archive_reader;

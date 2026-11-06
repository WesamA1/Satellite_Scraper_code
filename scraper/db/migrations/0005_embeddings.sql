-- Phase 3: image embeddings for the first-pass attribute model. Additive
-- only -- no existing table is touched. Never edit this file once applied
-- anywhere; future changes are new numbered migration files.
--
-- Keyed by the image file's SHA-256, not images.id: the old annotation
-- tool's images aren't in the images table, and its folder mode moves files
-- between folders, so the content hash is the one key that covers both and
-- survives a move. One row per (file content, backbone).

CREATE TABLE embeddings (
  sha256      TEXT NOT NULL,
  model       TEXT NOT NULL,
  dim         INTEGER NOT NULL,
  vector      BLOB NOT NULL,  -- dim float32s, L2-normalized
  created_at  TEXT NOT NULL,
  PRIMARY KEY (sha256, model)
);

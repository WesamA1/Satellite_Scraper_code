-- Phase 1, Task 2: initial schema.
-- Never edit this file once applied anywhere; future changes are new numbered
-- migration files (0002_..., 0003_...).

CREATE TABLE images (
  id                  INTEGER PRIMARY KEY,
  family              TEXT NOT NULL,
  path                TEXT NOT NULL UNIQUE,
  perceptual_hash     TEXT,
  width               INTEGER,
  height              INTEGER,
  duplicate_group_id  INTEGER,
  status              TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'rejected_filter', 'rejected_duplicate')),
  created_at          TEXT NOT NULL
);

CREATE INDEX idx_images_family ON images(family);

CREATE TABLE sources (
  id             INTEGER PRIMARY KEY,
  image_id       INTEGER NOT NULL UNIQUE REFERENCES images(id),
  source_module  TEXT NOT NULL,
  external_id    TEXT NOT NULL,
  source_url     TEXT NOT NULL,
  retrieved_at   TEXT NOT NULL,
  UNIQUE (source_module, external_id)
);

CREATE TABLE licenses (
  id                    INTEGER PRIMARY KEY,
  image_id              INTEGER NOT NULL UNIQUE REFERENCES images(id),
  license_short_name    TEXT NOT NULL,
  license_url           TEXT,
  author                TEXT,
  attribution_required  INTEGER,
  credit                TEXT,
  attribution_string    TEXT,
  verified_at           TEXT NOT NULL
);

CREATE TABLE predictions (
  id             INTEGER PRIMARY KEY,
  image_id       INTEGER NOT NULL REFERENCES images(id),
  attribute      TEXT NOT NULL,
  value          TEXT NOT NULL,
  confidence     REAL,
  model_version  TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  UNIQUE (image_id, attribute, model_version)
);

CREATE TABLE annotations (
  id             INTEGER PRIMARY KEY,
  image_id       INTEGER NOT NULL REFERENCES images(id),
  attribute      TEXT NOT NULL,
  value          TEXT NOT NULL,
  annotator      TEXT NOT NULL,
  guide_version  TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  UNIQUE (image_id, attribute, annotator)
);

CREATE TABLE rate_limits (
  host              TEXT PRIMARY KEY,
  cooldown_until    TEXT,
  last_retry_after  INTEGER,
  updated_at        TEXT NOT NULL
);

-- Not one of the brief's 6 named tables; added so rejection counts (license gate,
-- empty-search negative cache) have somewhere durable to live. See the Task 2 plan
-- for the full justification.
CREATE TABLE rejections (
  id             INTEGER PRIMARY KEY,
  family         TEXT NOT NULL,
  source_module  TEXT NOT NULL,
  external_id    TEXT,
  reason         TEXT NOT NULL,
  detail         TEXT,
  rejected_at    TEXT NOT NULL
);

CREATE INDEX idx_rejections_family_source ON rejections(family, source_module, reason);

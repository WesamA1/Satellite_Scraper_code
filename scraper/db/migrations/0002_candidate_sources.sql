-- Phase 2: candidate_sources. Additive only -- no existing table is touched.
-- Never edit this file once applied anywhere; future changes are new numbered
-- migration files.

CREATE TABLE candidate_sources (
  id                  INTEGER PRIMARY KEY,
  prompt              TEXT NOT NULL,
  name                TEXT NOT NULL,
  url                 TEXT NOT NULL,
  api_url             TEXT,
  license_model       TEXT,
  license_verifiable  INTEGER NOT NULL DEFAULT 0,
  notes               TEXT,
  generated_path      TEXT,
  status              TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'approved', 'rejected', 'code_generated', 'promoted')),
  discovered_at       TEXT NOT NULL,
  reviewed_at         TEXT
);

CREATE INDEX idx_candidate_sources_status ON candidate_sources(status);

-- The Status page's overview. Additive only. Never edit this file once
-- applied anywhere; future changes are new numbered migration files.
--
-- scrape_runs keeps each scrape's summary (it used to exist only in that
-- run's log), so recent runs can be seen at a glance.
-- images.filtered_at records when the CLIP filter scored an image, so
-- "new images that haven't been through the filter" can be counted. Images
-- from before this column existed stay NULL.

CREATE TABLE scrape_runs (
  id             INTEGER PRIMARY KEY,
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  status         TEXT NOT NULL CHECK (status IN ('running', 'finished', 'stopped', 'failed')),
  families       TEXT NOT NULL,  -- JSON list
  sources        TEXT NOT NULL,  -- JSON list
  families_done  INTEGER NOT NULL DEFAULT 0,
  images_added   INTEGER NOT NULL DEFAULT 0,
  summary        TEXT,           -- JSON: per source {images, empty_searches, no_license, not_an_image}
  error          TEXT
);

ALTER TABLE images ADD COLUMN filtered_at TEXT;

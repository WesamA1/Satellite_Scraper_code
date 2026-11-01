-- Phase 2: discovery_queue. Additive only -- no existing table is touched.
-- Never edit this file once applied anywhere; future changes are new numbered
-- migration files.
--
-- Backs the GUI's auto-discovery feature: one row per satellite family,
-- tracking whether that family has already had a source-discovery search run
-- for it. searched_at stays NULL until the batch runner processes that row;
-- candidate_count records how many candidates that search produced (0 is a
-- legitimate "searched, found nothing" result, distinct from "never searched").

CREATE TABLE discovery_queue (
  family           TEXT PRIMARY KEY,
  searched_at      TEXT,
  candidate_count  INTEGER NOT NULL DEFAULT 0
);

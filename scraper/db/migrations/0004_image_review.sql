-- Phase 2: human review tracking. Additive only -- no existing table is
-- touched. Never edit this file once applied anywhere; future changes are
-- new numbered migration files.
--
-- Lets the review queue tell "still needs a human look" apart from
-- "already reviewed and kept active" -- status alone can't do that, since
-- a kept image and a never-reviewed image are both 'active'.

ALTER TABLE images ADD COLUMN reviewed_at TEXT;

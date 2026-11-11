-- The filter's score for an image (0 to 1, higher = more likely a
-- satellite). Additive only. Never edit this file once applied anywhere;
-- future changes are new numbered migration files.
--
-- Review uses it to show the most likely satellites first and to tell
-- which waiting images haven't been screened yet (NULL).

ALTER TABLE images ADD COLUMN filter_score REAL;

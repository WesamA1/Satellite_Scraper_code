-- Where an image came from. Additive only -- existing rows are all scraped,
-- which is the default. Never edit this file once applied anywhere; future
-- changes are new numbered migration files.
--
-- 'imported' images come from a data/ folder (the old annotation tool's
-- images, with its CSV's answers). When an imported image and a scraped one
-- are the same file, the imported copy is kept: it's the one people already
-- annotated.

ALTER TABLE images ADD COLUMN origin TEXT NOT NULL DEFAULT 'scraped'
  CHECK (origin IN ('scraped', 'imported'));

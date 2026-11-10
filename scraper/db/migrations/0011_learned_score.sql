-- The trained screener's verdict on an image. Additive only. Never edit this
-- file once applied anywhere; future changes are new numbered migration files.
--
-- The filter's prompt score (filter_score) is the same for everyone. The
-- trained screener (scraper/ml/screener.py) learns from this project's own
-- Review decisions. learned_score is its margin: how far the image is above
-- (kept) or below (set aside) the screener's cut-off, so "below 0" keeps
-- meaning "the trained screener set it aside" after a retrain moves the
-- cut-off. NULL: the trained screener wasn't on when the image was screened.

ALTER TABLE images ADD COLUMN learned_score REAL;

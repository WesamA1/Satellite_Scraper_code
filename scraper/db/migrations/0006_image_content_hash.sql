-- Exact-copy detection. Additive only -- no existing column changes. Never
-- edit this file once applied anywhere; future changes are new numbered
-- migration files.
--
-- perceptual_hash says two images *look* the same; two different photos
-- can share one (two dark shots of similar satellites). content_sha256 says
-- two files are byte-for-byte the same file, which is the only thing the
-- dedupe stage is allowed to act on without a human.

ALTER TABLE images ADD COLUMN content_sha256 TEXT;

CREATE INDEX idx_images_content_sha256 ON images(content_sha256);

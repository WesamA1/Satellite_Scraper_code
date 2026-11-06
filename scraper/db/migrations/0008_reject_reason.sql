-- Why an image was rejected. Additive only. Never edit this file once
-- applied anywhere; future changes are new numbered migration files.
--
-- Review's reject buttons say why ('several_satellites', 'not_recognizable',
-- 'not_a_satellite', 'other'); the filter and the duplicate handling record
-- their own ('filter', 'duplicate'). The human reasons are what a future
-- screening classifier learns from. Existing rejects stay NULL (unknown).

ALTER TABLE images ADD COLUMN reject_reason TEXT;

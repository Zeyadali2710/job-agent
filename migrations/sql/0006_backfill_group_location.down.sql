-- Irreversible by design: the pre-backfill values were wrong, and restoring
-- them would mean re-corrupting the column. Downgrading this revision leaves
-- the corrected data in place.
SELECT 1;

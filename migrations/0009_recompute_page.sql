-- Recompute sessions.page from sessions.url using classify.page_path
-- (same as ingest and write-ups). The Python step lives in capture.db;
-- SQL cannot share urllib.parse with those callers.
--
-- 0008's regexp treated an unreadable URL as `/`, which collapsed the
-- page picker onto one homepage facet. This overwrites that approximation.
SELECT 1;

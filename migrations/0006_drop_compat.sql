-- Sampling now queries public.sessions / public.orders directly.
-- Existing installs drop the temporary SQLite-shaped view; fresh installs
-- still create it in 0002/0005, then drop it here.
DROP SCHEMA IF EXISTS compat CASCADE;

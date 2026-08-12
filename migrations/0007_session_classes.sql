-- Class labels materialised at ingest so cohort sampling is plain SQL WHERE.
-- Classifiers live in capture/classify.py; these columns are their outputs.
-- Forward-only: existing rows keep defaults (outcome='rest', null device/page)
-- until re-ingested; no backfill of disposable old data.

ALTER TABLE sessions
    ADD COLUMN outcome text NOT NULL DEFAULT 'rest',
    ADD COLUMN device_class text,
    ADD COLUMN page text,
    ADD COLUMN utm_source text,
    ADD COLUMN utm_medium text,
    ADD COLUMN utm_campaign text,
    ADD COLUMN utm_content text,
    ADD COLUMN has_fbclid boolean NOT NULL DEFAULT false,
    ADD COLUMN is_meta boolean NOT NULL DEFAULT false;

ALTER TABLE sessions
    ADD CONSTRAINT sessions_outcome_check
        CHECK (outcome IN ('purchased', 'checkout', 'rest')),
    ADD CONSTRAINT sessions_device_class_check
        CHECK (device_class IS NULL OR device_class IN ('mobile', 'desktop'));

CREATE INDEX sessions_outcome
    ON sessions (project_id, outcome, last_seen DESC);
CREATE INDEX sessions_device_class
    ON sessions (project_id, device_class, last_seen DESC)
    WHERE device_class IS NOT NULL;
CREATE INDEX sessions_page
    ON sessions (project_id, page, last_seen DESC)
    WHERE page IS NOT NULL;
CREATE INDEX sessions_utm_source
    ON sessions (project_id, utm_source, last_seen DESC)
    WHERE utm_source IS NOT NULL;
CREATE INDEX sessions_is_meta
    ON sessions (project_id, is_meta, last_seen DESC)
    WHERE is_meta;

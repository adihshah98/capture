-- 0007 added class columns with no backfill. Ingest writes them on new
-- sessions; this copies the JSON / orders already on old rows into those
-- columns so sampling can stay a SQL WHERE — no per-query fallback.
--
-- Mirrors capture.classify (device, page, outcome) and
-- capture.attribution.attribution_classes (utm_*, has_fbclid, is_meta).
-- COALESCE keeps any value ingest already wrote.

-- Purchased / checkout live on orders. The column defaulted to 'rest'.
UPDATE sessions s
SET outcome = CASE
        WHEN o.purchased_at IS NOT NULL THEN 'purchased'
        WHEN o.checkout_started_at IS NOT NULL THEN 'checkout'
        ELSE s.outcome
    END
FROM orders o
WHERE o.project_id = s.project_id
  AND o.session_id = s.session_id
  AND s.outcome IS DISTINCT FROM CASE
        WHEN o.purchased_at IS NOT NULL THEN 'purchased'
        WHEN o.checkout_started_at IS NOT NULL THEN 'checkout'
        ELSE s.outcome
    END;

WITH src AS (
    SELECT
        s.project_id,
        s.session_id,
        CASE
            WHEN jsonb_typeof(s.device -> 'width') = 'number'
                 AND (s.device ->> 'width') ~ '^[0-9]+$'
                 AND (s.device ->> 'width')::int > 0
                 AND (s.device ->> 'width')::int < 768 THEN 'mobile'
            WHEN jsonb_typeof(s.device -> 'width') = 'number'
                 AND (s.device ->> 'width') ~ '^[0-9]+$'
                 AND (s.device ->> 'width')::int >= 768 THEN 'desktop'
            ELSE NULL
        END AS device_class,
        CASE
            WHEN s.url IS NULL OR btrim(s.url) = '' THEN NULL
            WHEN s.url ~* '^(about:|data:|javascript:|blob:|null|undefined)$' THEN NULL
            ELSE COALESCE(
                NULLIF(
                    regexp_replace(
                        NULLIF(
                            regexp_replace(
                                regexp_replace(
                                    btrim(s.url),
                                    '^[a-zA-Z][a-zA-Z0-9+.-]*://[^/?#]+',
                                    ''
                                ),
                                '[?#].*$',
                                ''
                            ),
                            ''
                        ),
                        '/$',
                        ''
                    ),
                    ''
                ),
                '/'
            )
        END AS page,
        COALESCE(
            NULLIF(lower(btrim(left(s.attribution ->> 'utm_source', 256))), ''),
            NULLIF(lower(btrim(left(
                replace((regexp_match(split_part(s.url, '#', 1), '[?&]utm_source=([^&]*)'))[1], '+', ' '),
                256
            ))), ''),
            NULLIF(lower(btrim(left(
                replace((regexp_match(substring(s.url FROM '#(.*)'), '(?:^|&)utm_source=([^&]*)'))[1], '+', ' '),
                256
            ))), '')
        ) AS utm_source,
        COALESCE(
            NULLIF(lower(btrim(left(s.attribution ->> 'utm_medium', 256))), ''),
            NULLIF(lower(btrim(left(
                replace((regexp_match(split_part(s.url, '#', 1), '[?&]utm_medium=([^&]*)'))[1], '+', ' '),
                256
            ))), ''),
            NULLIF(lower(btrim(left(
                replace((regexp_match(substring(s.url FROM '#(.*)'), '(?:^|&)utm_medium=([^&]*)'))[1], '+', ' '),
                256
            ))), '')
        ) AS utm_medium,
        COALESCE(
            NULLIF(lower(btrim(left(s.attribution ->> 'utm_campaign', 256))), ''),
            NULLIF(lower(btrim(left(
                replace((regexp_match(split_part(s.url, '#', 1), '[?&]utm_campaign=([^&]*)'))[1], '+', ' '),
                256
            ))), ''),
            NULLIF(lower(btrim(left(
                replace((regexp_match(substring(s.url FROM '#(.*)'), '(?:^|&)utm_campaign=([^&]*)'))[1], '+', ' '),
                256
            ))), '')
        ) AS utm_campaign,
        COALESCE(
            NULLIF(lower(btrim(left(s.attribution ->> 'utm_content', 256))), ''),
            NULLIF(lower(btrim(left(
                replace((regexp_match(split_part(s.url, '#', 1), '[?&]utm_content=([^&]*)'))[1], '+', ' '),
                256
            ))), ''),
            NULLIF(lower(btrim(left(
                replace((regexp_match(substring(s.url FROM '#(.*)'), '(?:^|&)utm_content=([^&]*)'))[1], '+', ' '),
                256
            ))), '')
        ) AS utm_content,
        COALESCE(
            (s.attribution ? 'fbclid'
                AND s.attribution -> 'fbclid' IS NOT NULL
                AND s.attribution ->> 'fbclid' NOT IN ('', 'false', '0')),
            false
        ) OR COALESCE(s.url ~ '[?&#]fbclid=', false) AS has_fbclid
    FROM sessions s
)
UPDATE sessions s
SET
    device_class = COALESCE(s.device_class, src.device_class),
    page         = COALESCE(s.page, src.page),
    utm_source   = COALESCE(s.utm_source, src.utm_source),
    utm_medium   = COALESCE(s.utm_medium, src.utm_medium),
    utm_campaign = COALESCE(s.utm_campaign, src.utm_campaign),
    utm_content  = COALESCE(s.utm_content, src.utm_content),
    has_fbclid   = COALESCE(s.has_fbclid, false) OR COALESCE(src.has_fbclid, false),
    -- `NULL IN (...)` is NULL, not false; OR that onto a boolean NULLs is_meta
    -- and trips the NOT NULL constraint on untagged sessions (no utm_source).
    is_meta      = COALESCE(s.is_meta, false)
        OR COALESCE(s.has_fbclid, false)
        OR COALESCE(src.has_fbclid, false)
        OR COALESCE(s.utm_source, src.utm_source, '') IN (
            'facebook', 'meta', 'fb', 'instagram', 'ig'
        )
FROM src
WHERE s.project_id = src.project_id
  AND s.session_id = src.session_id;

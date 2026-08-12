-- Cohort ORDER BY used to call compat.json_extract(interactions, '$.clicks'),
-- round-tripping the integer sessions.clicks column out to JSON text and back.
-- Expose clicks (and rage) on the view so sampling can sort on the real column,
-- then drop the SQLite-shaped shim.

DROP FUNCTION IF EXISTS compat.json_extract(text, text);

DO $$
DECLARE
    app_role text := nullif(current_setting('capture.app_role', true), '');
    invoker  boolean := current_setting('server_version_num')::int >= 150000;
BEGIN
    IF NOT invoker AND app_role IS NULL THEN
        RAISE EXCEPTION
            'unsafe compat view: this server is % and CAPTURE_DB_APP_ROLE is unset. '
            'Use PostgreSQL 15+, or name the non-superuser role the app connects as.',
            current_setting('server_version');
    END IF;
    EXECUTE 'DROP VIEW IF EXISTS compat.sessions';
    EXECUTE 'CREATE VIEW compat.sessions'
        || CASE WHEN invoker THEN ' WITH (security_invoker = true)' ELSE '' END || ' AS
        SELECT
            s.project_id::text AS pool,
            s.session_id,
            s.first_seen,
            s.last_seen,
            s.last_active,
            s.duration,
            s.windows,
            s.chunk_count,
            s.bytes,
            s.clicks,
            s.rage,
            s.visitor_id,
            s.url,
            CASE WHEN o.session_id IS NULL THEN NULL ELSE
                jsonb_strip_nulls(jsonb_build_object(
                    ''checkout_started_at'', o.checkout_started_at,
                    ''purchased_at'', o.purchased_at,
                    ''order_id'', o.order_id,
                    ''total'', o.total,
                    ''currency'', o.currency
                ))::text
            END AS "order",
            jsonb_build_object(''clicks'', s.clicks, ''rage'', s.rage)::text AS interactions,
            s.device::text AS device,
            s.attribution::text AS attribution
        FROM sessions s
        LEFT JOIN orders o
               ON o.project_id = s.project_id AND o.session_id = s.session_id';
    IF app_role IS NOT NULL THEN
        EXECUTE format('ALTER VIEW compat.sessions OWNER TO %I', app_role);
    END IF;
END $$;

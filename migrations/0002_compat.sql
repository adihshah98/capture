-- analyze.py is owned by someone else and its cohort/facet queries are written
-- against the pre-Postgres SQLite index: a flat `sessions` table with `pool`
-- and JSON-text `order`/`interactions`. Rather than reshape their file, put a
-- schema in front that still looks like that. The connection handed to those
-- functions runs with search_path = compat, public.
--
-- `clicks` / `rage` are exposed as plain columns (see also 0005) so ORDER BY
-- can use the integer without round-tripping through the interactions JSON.
--
-- Delete this whole migration once those five functions are ported.

CREATE SCHEMA compat;

-- SQLite handed back JSON columns as TEXT, and _parse_index_json calls
-- json.loads on them. Returning jsonb would make psycopg produce dicts, which
-- that helper silently turns into None — so every column is cast back to text.
-- A view executes as its owner, so one owned by a superuser bypasses row level
-- security on the tables underneath and quietly hands one customer another's
-- sessions. Two ways to stay inside RLS: security_invoker (PostgreSQL 15+), or
-- an owner that is itself subject to RLS. Refuse to build the view if neither
-- is available rather than build a leaky one.
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

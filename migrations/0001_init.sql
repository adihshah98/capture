-- gen_random_uuid() is core since PostgreSQL 13, so no extension is needed.
-- That keeps the schema installable without elevated privileges.

CREATE TABLE projects (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name           text NOT NULL,
    org_id         text,
    domains        jsonb NOT NULL DEFAULT '[]'::jsonb,
    config         jsonb,
    storage_prefix text,
    kms_key_arn    text,
    bytes_used     bigint NOT NULL DEFAULT 0,
    -- Single-tenant installs ingest without a token. Exactly one project may
    -- absorb those, which is what the legacy data/sessions pool becomes.
    is_default     boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX projects_org ON projects (org_id) WHERE org_id IS NOT NULL;
CREATE UNIQUE INDEX projects_single_default ON projects ((true)) WHERE is_default;

-- Ingest tokens are public in the customer's page source, so they are a
-- credential rather than an identity. Keeping them separate lets one rotate
-- or be revoked without rewriting every row and object keyed to the project.
CREATE TABLE project_tokens (
    token      text PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    label      text,
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);
CREATE INDEX project_tokens_project ON project_tokens (project_id);

CREATE TABLE sessions (
    project_id  uuid NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    session_id  text NOT NULL,
    first_seen  bigint NOT NULL,
    last_seen   bigint NOT NULL,
    last_active bigint NOT NULL DEFAULT 0,
    duration    bigint,
    windows     integer NOT NULL DEFAULT 1,
    chunk_count integer NOT NULL DEFAULT 0,
    bytes       bigint NOT NULL DEFAULT 0,
    clicks      integer NOT NULL DEFAULT 0,
    rage        integer NOT NULL DEFAULT 0,
    visitor_id  text,
    url         text,
    device      jsonb,
    attribution jsonb,
    PRIMARY KEY (project_id, session_id)
);
CREATE INDEX sessions_recency ON sessions (project_id, last_seen DESC, session_id DESC);
CREATE INDEX sessions_visitor ON sessions (project_id, visitor_id) WHERE visitor_id IS NOT NULL;

CREATE TABLE session_windows (
    project_id  uuid NOT NULL,
    session_id  text NOT NULL,
    window_id   text NOT NULL,
    first_seen  bigint NOT NULL,
    last_seen   bigint NOT NULL,
    last_active bigint NOT NULL DEFAULT 0,
    bytes       bigint NOT NULL DEFAULT 0,
    -- Carries the activity scanner's href/nav position between chunks; this is
    -- what window-state.json held that was not simple accumulation. Without
    -- href, the first type-4 event of every chunk reads as a navigation.
    nav         bigint,
    href        text,
    PRIMARY KEY (project_id, session_id, window_id),
    FOREIGN KEY (project_id, session_id) REFERENCES sessions (project_id, session_id) ON DELETE CASCADE
);

CREATE TABLE orders (
    project_id         uuid NOT NULL,
    session_id         text NOT NULL,
    checkout_started_at bigint,
    purchased_at       bigint,
    order_id           text,
    total              double precision,
    currency           text,
    PRIMARY KEY (project_id, session_id),
    FOREIGN KEY (project_id, session_id) REFERENCES sessions (project_id, session_id) ON DELETE CASCADE
);
CREATE INDEX orders_purchased ON orders (project_id, purchased_at) WHERE purchased_at IS NOT NULL;

CREATE TABLE cohorts (
    project_id uuid NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    cohort_id  text NOT NULL,
    body       jsonb NOT NULL,
    updated_at bigint NOT NULL,
    PRIMARY KEY (project_id, cohort_id)
);

CREATE TABLE cohort_cuts (
    project_id uuid NOT NULL,
    cohort_id  text NOT NULL,
    cut_key    text NOT NULL,
    body       jsonb NOT NULL,
    PRIMARY KEY (project_id, cohort_id, cut_key),
    FOREIGN KEY (project_id, cohort_id) REFERENCES cohorts (project_id, cohort_id) ON DELETE CASCADE
);

CREATE TABLE analyses (
    project_id uuid NOT NULL,
    session_id text NOT NULL,
    body       jsonb NOT NULL,
    created_at bigint NOT NULL,
    PRIMARY KEY (project_id, session_id),
    FOREIGN KEY (project_id, session_id) REFERENCES sessions (project_id, session_id) ON DELETE CASCADE
);

CREATE TABLE analysis_jobs (
    project_id uuid NOT NULL,
    session_id text NOT NULL,
    status     text NOT NULL,
    error      text,
    updated_at bigint NOT NULL,
    PRIMARY KEY (project_id, session_id),
    FOREIGN KEY (project_id, session_id) REFERENCES sessions (project_id, session_id) ON DELETE CASCADE
);

-- Tenant isolation is a database invariant, not a convention every query has to
-- remember. Without app.project_id set, a scoped query returns nothing rather
-- than another customer's recordings. FORCE makes this bind the table owner too.
CREATE OR REPLACE FUNCTION current_project() RETURNS uuid
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('app.project_id', true), '')::uuid
$$;

CREATE OR REPLACE FUNCTION is_admin_scope() RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT coalesce(nullif(current_setting('app.admin', true), ''), 'off') = 'on'
$$;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'sessions', 'session_windows', 'orders',
        'cohorts', 'cohort_cuts', 'analyses', 'analysis_jobs'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING '
            '(is_admin_scope() OR project_id = current_project()) '
            'WITH CHECK (is_admin_scope() OR project_id = current_project())', t
        );
    END LOOP;
END $$;

-- Cohort state, adopting the model analyze_store.py arrived at in SQLite
-- rather than the jsonb blob 0001 guessed at. Membership is its own table so a
-- session's status can be updated a row at a time instead of rewriting the
-- document, and the columns promoted out of the blob are the ones that get
-- filtered on. Everything else rides in `fields`, which keeps the sampling
-- vocabulary free to change without a migration.

DROP TABLE IF EXISTS cohort_cuts;
DROP TABLE IF EXISTS cohorts;

CREATE TABLE cohorts (
    project_id uuid NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    cohort_id  text NOT NULL,
    label      text,
    status     text NOT NULL DEFAULT 'idle',
    created_at bigint NOT NULL DEFAULT 0,
    updated_at bigint NOT NULL DEFAULT 0,
    batch      jsonb,
    fields     jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (project_id, cohort_id)
);
CREATE INDEX cohorts_recency ON cohorts (project_id, created_at DESC);

-- Resuming a batch after a restart was a directory scan reading every cohort
-- file; this makes it an index lookup.
CREATE INDEX cohorts_resumable ON cohorts (status) WHERE batch IS NOT NULL;

CREATE TABLE cohort_sessions (
    project_id uuid NOT NULL,
    cohort_id  text NOT NULL,
    session_id text NOT NULL,
    position   integer NOT NULL,
    status     text,
    analysis   text,
    error      text,
    fields     jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (project_id, cohort_id, session_id),
    FOREIGN KEY (project_id, cohort_id)
        REFERENCES cohorts (project_id, cohort_id) ON DELETE CASCADE
);

-- forget_session deletes by session across every cohort, and the retention
-- sweep walks that path once per expired session. The primary key leads with
-- cohort_id, so without this it is a sequential scan.
CREATE INDEX cohort_sessions_by_session ON cohort_sessions (project_id, session_id);

-- Deliberately no foreign key to sessions. It would be enforceable here, unlike
-- in SQLite, but it would also start rejecting a write_cohort whose sampling
-- raced a delete — a failure the current code absorbs by dropping the row.
-- Membership is still reaped explicitly at every delete path.

CREATE TABLE cohort_cuts (
    project_id uuid NOT NULL,
    cohort_id  text NOT NULL,
    cut_key    text NOT NULL,
    cut        jsonb NOT NULL,
    PRIMARY KEY (project_id, cohort_id, cut_key)
);

-- No foreign key to cohorts, matching the SQLite original: a cut can be
-- cached before its cohort row exists, and adding one here would turn that
-- into an error. delete_cohort removes them explicitly instead.

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['cohorts', 'cohort_sessions', 'cohort_cuts'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING '
            '(is_admin_scope() OR project_id = current_project()) '
            'WITH CHECK (is_admin_scope() OR project_id = current_project())', t
        );
    END LOOP;
END $$;

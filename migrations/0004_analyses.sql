-- Analyses and job status move off analysis.json and analysis.status.json.
--
-- The foreign keys to sessions come off. They bought a cascade that the delete
-- paths already perform explicitly, and charged for it with a write-time
-- constraint: a write-up cannot be recorded for a session that has no row,
-- which the analysis unit tests rely on being able to do. The same trade came
-- up with cohort_cuts. Deletion stays visible in the code doing the deleting.

ALTER TABLE analyses DROP CONSTRAINT IF EXISTS analyses_project_id_session_id_fkey;
ALTER TABLE analysis_jobs DROP CONSTRAINT IF EXISTS analysis_jobs_project_id_session_id_fkey;

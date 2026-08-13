"""Session analysis: replay the session headlessly, then ask Gemini about it.

The extractor drives the same CapturePlayer the replay UI uses, so animations
are frozen and recorded scroll offsets are re-applied exactly as they are on
screen. Stepping that player yields a frame per second of activity — the page
as the visitor saw it change, rather than a single end-state snapshot.

Cohort lifecycle (sample, Batch/sync job, resume) lives in `cohorts.py`. A
cohort is immutable once sampled, so a "cut" across it can be analysed once and
cached against it (`refresh=1` busts that cache).

Cohort state lives in Postgres (`0003_cohorts.sql`). Sampling defaults are in
spec.py, not here.

The web layer reaches this package through `capture.analysis_port`. Tests import
owner modules (`gemini`, `session`, `cuts`, `cohorts`, `runtime`, `storage`, …)
and patch at the definition site. This file is not a facade: it does not
re-export those names and does not forward attribute access.
"""

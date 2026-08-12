"""Shared analysis config and the extract/model concurrency gates.

Env-derived settings and the semaphore pair live here so every orchestrator
imports one module rather than the package parent. `_gates()` must stay in a
single place: split across modules and each gets its own limit.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

# The repo root: EXTRACTOR, the extractor subprocess cwd and the default data
# dir are all repo-relative, and this file is capture/analyze/runtime.py, so
# it climbs two levels rather than the one a top-level module would.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
ANALYZE_TIMEOUT_S = float(os.environ.get("ANALYZE_TIMEOUT_S", "300"))

# Frames come one per second of activity; dead air is skipped, so a long
# session with a little interaction stays cheap.
ANALYZE_FPS = float(os.environ.get("ANALYZE_FPS", "1"))
# Capped by default: activity-based sampling keeps a typical session well under
# this, but a continuously-active one grows a frame per second without bound
# (~200 frames for 200s), and at cohort scale those JPEGs are the OOM the batch
# path was rewritten to survive. Set 0 to disable the cap.
ANALYZE_MAX_FRAMES = int(os.environ.get("ANALYZE_MAX_FRAMES", "90"))

ANALYSIS_ENABLED = bool(GEMINI_API_KEY)

# Extraction and model calls contend for different resources, so they get
# different gates. Reconstruction drives a headless Chromium and is memory
# bound — one at a time, or a small box starts swapping. The Gemini calls are
# pure network wait, so a few can overlap and the cohort loop stops spending
# most of its wall clock idle.
ANALYZE_EXTRACT_CONCURRENCY = max(1, int(os.environ.get("ANALYZE_EXTRACT_CONCURRENCY", "1")))
ANALYZE_MODEL_CONCURRENCY = max(1, int(os.environ.get("ANALYZE_MODEL_CONCURRENCY", "3")))

# Cohort write-ups go through Gemini Batch (≈50% cheaper). Single-session
# Analyze stays on the interactive API. Set ANALYZE_BATCH=0 to force sync.
ANALYZE_BATCH = os.environ.get("ANALYZE_BATCH", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
ANALYZE_BATCH_POLL_S = float(os.environ.get("ANALYZE_BATCH_POLL_S", "15"))
# Batch is slow by design (the 50% discount buys a queue, not a latency SLA),
# so this is a stuck-job backstop rather than a deadline anyone should hit.
# The job name stays on disk when it trips, so a resume picks the job back up
# instead of re-billing the cohort. 0 waits forever, as before.
ANALYZE_BATCH_MAX_WAIT_S = float(os.environ.get("ANALYZE_BATCH_MAX_WAIT_S", str(24 * 3600)))
# A poll that raises is usually a blip, and escalating one would cost a re-run
# of the whole cohort, so tolerate a few in a row before giving up.
ANALYZE_BATCH_POLL_MAX_ERRORS = max(1, int(os.environ.get("ANALYZE_BATCH_POLL_MAX_ERRORS", "5")))

# Created lazily: a Semaphore binds to the running loop, and this module is
# imported before the server's loop exists.
_extract_gate: asyncio.Semaphore | None = None
_model_gate: asyncio.Semaphore | None = None
_gate_loop: asyncio.AbstractEventLoop | None = None

EXTRACTOR = BASE_DIR / "src" / "analyze" / "extract.mjs"
REPLAYER_BUNDLE = BASE_DIR / "src" / "analyze" / "replayer.bundle.js"


def _gates() -> tuple:
    """(extract, model) semaphores bound to the running loop."""
    global _extract_gate, _model_gate, _gate_loop
    loop = asyncio.get_running_loop()
    if _gate_loop is not loop or _extract_gate is None or _model_gate is None:
        _extract_gate = asyncio.Semaphore(ANALYZE_EXTRACT_CONCURRENCY)
        _model_gate = asyncio.Semaphore(ANALYZE_MODEL_CONCURRENCY)
        _gate_loop = loop
    return _extract_gate, _model_gate


def runtime_present() -> bool:
    """Whether the headless reconstruction can run: node, the extractor, the bundle."""
    return bool(shutil.which("node") and EXTRACTOR.is_file() and REPLAYER_BUNDLE.is_file())

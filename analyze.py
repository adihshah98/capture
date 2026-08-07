"""Session analysis: replay the session headlessly, then ask Gemini about it.

The extractor drives the same CapturePlayer the replay UI uses, so animations
are frozen and recorded scroll offsets are re-applied exactly as they are on
screen. Stepping that player yields a frame per second of activity — the page
as the visitor saw it change, rather than a single end-state snapshot.

Also owns cohorts: pick N purchased + N all sessions, persist the sample under
`cohorts/<id>.json`, and leave the server to queue per-session analysis against
it. A cohort is immutable once sampled, so a "cut" across it (Meta vs Google,
campaign A vs B) can be analysed by the model once and cached forever beside it.
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent

ANALYSIS_FILE = "analysis.json"
ANALYSIS_STATUS_FILE = "analysis.status.json"
ANALYSIS_DIR = ".analysis"
# Pre-multi-cohort layout: one cohort.json per pool, migrated on first read.
COHORT_FILE = "cohort.json"
COHORTS_DIR = "cohorts"
CUTS_SUFFIX = ".cuts.json"
# Same shape as server.SAFE_CHUNK — keep local so the CLI does not import server.
SAFE_CHUNK = re.compile(r"^\d{1,16}(-[a-f0-9]{1,8})?-[a-z0-9]{6}\.json$")
# Cohort ids are ours to mint ("cohort-<ms>"), but they land in filenames, so
# validate rather than trust anything arriving from a URL.
SAFE_COHORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
ANALYZE_TIMEOUT_S = float(os.environ.get("ANALYZE_TIMEOUT_S", "300"))

# Frames come one per second of activity; dead air is skipped, so a long
# session with a little interaction stays cheap. 0 leaves the count uncapped.
ANALYZE_FPS = float(os.environ.get("ANALYZE_FPS", "1"))
ANALYZE_MAX_FRAMES = int(os.environ.get("ANALYZE_MAX_FRAMES", "0"))

ANALYSIS_ENABLED = bool(GEMINI_API_KEY)

# Extraction and model calls contend for different resources, so they get
# different gates. Reconstruction drives a headless Chromium and is memory
# bound — one at a time, or a small box starts swapping. The Gemini calls are
# pure network wait, so a few can overlap and the cohort loop stops spending
# most of its wall clock idle.
ANALYZE_EXTRACT_CONCURRENCY = max(1, int(os.environ.get("ANALYZE_EXTRACT_CONCURRENCY", "1")))
ANALYZE_MODEL_CONCURRENCY = max(1, int(os.environ.get("ANALYZE_MODEL_CONCURRENCY", "3")))

# Created lazily: a Semaphore binds to the running loop, and this module is
# imported before the server's loop exists (and reused by the sync CLI).
_extract_gate: Optional[asyncio.Semaphore] = None
_model_gate: Optional[asyncio.Semaphore] = None
_gate_loop: Optional[asyncio.AbstractEventLoop] = None


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

SYSTEM_PROMPT = """You analyse one recorded web session for a product team.

You receive a measured digest and a chronological sequence of frames from the
session replay, sampled about once per second through the parts of the session
where something was happening. Each frame is labelled [MM:SS]. A jump between
consecutive timestamps means the page sat untouched, not that something is
missing.

Ground every claim in the digest and the frames. Never invent a number, a page,
or an element you cannot see. If the warnings mention assets that failed to
load, do not describe what those parts of the page look like.

The frames prove what was displayed and what changed. They do not prove where
the visitor's eyes went. Say "on screen" or "reached", not "looked at" or
"read". This distinction matters — do not blur it.

Write four short markdown sections:

## What they did
The path through the session in order, citing [MM:SS]. What the page showed,
what they scrolled past, what opened or changed.

## Where they slowed down
Stretches where the page sat on one view, and what was on screen then. Cite the
timestamps. Note long jumps between frames as time the page was left untouched.

## Engagement and friction
What they clicked, in order, with timestamps. Call out rage clicks and repeated
interactions. Use the frames to say what the click did — or failed to do.

## What stands out
Two or three observations a product team could act on. If the evidence is thin,
say so instead of padding.

Keep it under 300 words. No preamble."""

def runtime_present() -> bool:
    """Whether the headless reconstruction can run: node, the extractor, the bundle."""
    return bool(
        shutil.which("node") and EXTRACTOR.is_file() and REPLAYER_BUNDLE.is_file()
    )


def analysis_paths(session_dir: Path) -> tuple:
    return session_dir / ANALYSIS_FILE, session_dir / ANALYSIS_DIR


def status_path(session_dir: Path) -> Path:
    return session_dir / ANALYSIS_STATUS_FILE


def read_analysis(session_dir: Path) -> Optional[dict]:
    path = session_dir / ANALYSIS_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def read_job(session_dir: Path) -> Optional[dict]:
    """In-progress or failed async analysis job, if any."""
    path = status_path(session_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_job(session_dir: Path, status: str, error: Optional[str] = None) -> dict:
    job = {
        "status": status,
        "updated_at": int(time.time() * 1000),
    }
    if error:
        job["error"] = error
    path = status_path(session_dir)
    path.write_text(json.dumps(job))
    return job


def clear_job(session_dir: Path) -> None:
    status_path(session_dir).unlink(missing_ok=True)


def clear_analysis(session_dir: Path) -> None:
    """Drop a finished result so a re-analyze starts clean."""
    (session_dir / ANALYSIS_FILE).unlink(missing_ok=True)
    # Sidecars from the retired theme/QA pass — inert now, but a re-analyze is
    # the natural moment to stop carrying them.
    for legacy in ("analysis.insights.json", "analysis.qa.json"):
        (session_dir / legacy).unlink(missing_ok=True)
    shutil.rmtree(session_dir / ANALYSIS_DIR, ignore_errors=True)


def cohorts_dir(pool: Path) -> Path:
    """Cohorts live beside the sessions directory (project root / data root)."""
    return pool.parent / COHORTS_DIR


def cohort_path(pool: Path, cohort_id: str) -> Path:
    return cohorts_dir(pool) / f"{safe_cohort_id(cohort_id)}.json"


def safe_cohort_id(cohort_id: str) -> str:
    """Cohort ids reach the filesystem, so keep them to a known-safe alphabet."""
    raw = str(cohort_id or "").strip()
    if not raw or not SAFE_COHORT_ID.match(raw):
        raise ValueError("bad cohort id")
    return raw


def _read_cohort_file(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def migrate_legacy_cohort(pool: Path) -> None:
    """Move a pre-multi-cohort cohort.json into the cohorts directory, once."""
    legacy = pool.parent / COHORT_FILE
    if not legacy.is_file():
        return
    data = _read_cohort_file(legacy)
    if data and isinstance(data.get("id"), str):
        try:
            target = cohort_path(pool, data["id"])
        except ValueError:
            legacy.unlink(missing_ok=True)
            return
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(data, indent=2))
    legacy.unlink(missing_ok=True)


def read_cohort(pool: Path, cohort_id: str) -> Optional[dict]:
    try:
        return _read_cohort_file(cohort_path(pool, cohort_id))
    except ValueError:
        return None


def write_cohort(pool: Path, cohort: dict) -> dict:
    cohort = {**cohort, "updated_at": int(time.time() * 1000)}
    path = cohort_path(pool, cohort["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cohort, indent=2))
    return cohort


def list_cohorts(pool: Path) -> list:
    """Every cohort in this pool, newest first. Membership only — no write-ups."""
    migrate_legacy_cohort(pool)
    out = []
    directory = cohorts_dir(pool)
    if not directory.is_dir():
        return out
    for path in directory.glob("*.json"):
        if path.name.endswith(CUTS_SUFFIX):
            continue
        data = _read_cohort_file(path)
        if data and isinstance(data.get("id"), str):
            out.append(data)
    out.sort(key=lambda c: c.get("created_at") or 0, reverse=True)
    return out


def latest_cohort(pool: Path) -> Optional[dict]:
    cohorts = list_cohorts(pool)
    return cohorts[0] if cohorts else None


def cuts_path(pool: Path, cohort_id: str) -> Path:
    return cohorts_dir(pool) / f"{safe_cohort_id(cohort_id)}{CUTS_SUFFIX}"


def read_cuts(pool: Path, cohort_id: str) -> dict:
    """All cached cut analyses for a cohort, keyed by cut_key()."""
    try:
        path = cuts_path(pool, cohort_id)
    except ValueError:
        return {}
    data = _read_cohort_file(path)
    return data if isinstance(data, dict) else {}


def read_cut(pool: Path, cohort_id: str, key: str) -> Optional[dict]:
    cut = read_cuts(pool, cohort_id).get(key)
    return cut if isinstance(cut, dict) else None


def write_cut(pool: Path, cohort_id: str, key: str, cut: dict) -> dict:
    """Cache one cut. A done cohort is immutable, so this never needs busting."""
    path = cuts_path(pool, cohort_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    cuts = read_cuts(pool, cohort_id)
    cuts[key] = cut
    path.write_text(json.dumps(cuts, indent=2))
    return cut


def enrich_cohort_status(pool: Path, cohort: dict) -> dict:
    """Refresh per-session status from analysis.json / analysis.status.json on disk."""
    sessions = []
    done = running = queued = errors = 0
    for entry in cohort.get("sessions") or []:
        row = dict(entry)
        # Insights text is attached on read for the API — never persist it here.
        row.pop("text", None)
        row.pop("standout", None)
        row.pop("model", None)
        row.pop("frame_count", None)
        # Derived from the index on read — never frozen into the cohort file.
        row.pop("attribution", None)
        row.pop("device", None)
        row.pop("outcome", None)
        session_dir = pool / row["session_id"]
        cached = read_analysis(session_dir)
        job = read_job(session_dir)
        if cached:
            row["status"] = "done"
            row["analysis"] = ANALYSIS_FILE
            row.pop("error", None)
            done += 1
        elif job and job.get("status") == "running":
            row["status"] = "running"
            running += 1
        elif job and job.get("status") == "error":
            row["status"] = "error"
            row["error"] = job.get("error") or row.get("error") or "analysis failed"
            errors += 1
        elif row.get("status") == "error":
            errors += 1
        elif row.get("status") == "done":
            done += 1
        else:
            row["status"] = "queued"
            queued += 1
        sessions.append(row)

    status = cohort.get("status") or "queued"
    if status not in ("queued", "running", "done", "error"):
        status = "queued"
    if running or (queued and status == "running"):
        status = "running"
    elif errors and done + errors >= len(sessions) and not queued and not running:
        status = "error" if done == 0 else "done"
    elif done >= len(sessions) and sessions:
        status = "done"
    elif not sessions:
        status = "done"

    return {
        **cohort,
        "status": status,
        "sessions": sessions,
        "progress": {
            "total": len(sessions),
            "done": done,
            "running": running,
            "queued": queued,
            "error": errors,
        },
    }


def standout_line(text: str) -> str:
    """First useful line under ## What stands out, else the first non-heading line."""
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines()]
    in_standout = False
    for line in lines:
        if re.match(r"^#{1,4}\s+what stands out\s*$", line, re.I):
            in_standout = True
            continue
        if in_standout:
            if re.match(r"^#{1,4}\s+", line):
                break
            bullet = re.match(r"^[-*]\s+(.*)$", line)
            if bullet and bullet.group(1).strip():
                return bullet.group(1).strip()
            if line:
                return line
    for line in lines:
        if line and not re.match(r"^#{1,4}\s+", line):
            return re.sub(r"^[-*]\s+", "", line)
    return ""


def attach_cohort_insights(pool: Path, cohort: dict) -> dict:
    """Attach analysis write-ups — response-only, never written to the cohort file."""
    sessions = []
    for entry in cohort.get("sessions") or []:
        row = dict(entry)
        if row.get("status") == "done":
            session_dir = pool / row["session_id"]
            cached = read_analysis(session_dir)
            if cached and isinstance(cached.get("text"), str) and cached["text"].strip():
                row["text"] = cached["text"]
                row["standout"] = standout_line(cached["text"])
                if cached.get("model"):
                    row["model"] = cached["model"]
                if cached.get("frame_count") is not None:
                    row["frame_count"] = cached["frame_count"]
        sessions.append(row)
    return {**cohort, "sessions": sessions}


META_SOURCES = frozenset({"facebook", "meta", "fb", "instagram", "ig"})

# Same threshold the session detail panel uses, so one session is never called
# mobile in one place and desktop in another.
MOBILE_MAX_WIDTH = 768
DEVICE_VALUES = ("mobile", "desktop")

# purchased ⊃ checkout: a purchase implies checkout was reached, so "checkout"
# here means reached checkout and did NOT purchase — the three are disjoint.
OUTCOME_VALUES = ("purchased", "checkout", "rest")


def device_class(device: Optional[dict]) -> Optional[str]:
    """mobile/desktop from the indexed viewport, or None when unknown."""
    if not isinstance(device, dict):
        return None
    width = device.get("width")
    if not isinstance(width, int) or width <= 0:
        return None
    return "mobile" if width < MOBILE_MAX_WIDTH else "desktop"


def outcome_class(order: Optional[dict]) -> str:
    """purchased | checkout (reached, did not buy) | rest."""
    if not isinstance(order, dict):
        return "rest"
    if order.get("purchased_at"):
        return "purchased"
    if order.get("checkout_started_at"):
        return "checkout"
    return "rest"


def is_meta_attribution(attribution: Optional[dict]) -> bool:
    attrs = attribution if isinstance(attribution, dict) else {}
    if attrs.get("fbclid"):
        return True
    source = str(attrs.get("utm_source") or "").strip().lower()
    return source in META_SOURCES


def normalize_slice(spec: Optional[dict]) -> dict:
    """Normalize a slice: any combination of outcome, device and attribution.

    Keys: outcome (list of purchased|checkout|rest), device (list of
    mobile|desktop), utm_source, utm_medium, utm_campaign, utm_content, fbclid.
    utm_source="meta" matches Meta traffic (fbclid or a known Meta source).

    Constraints are ANDed across keys and ORed within a key, so
    {"outcome": ["purchased", "checkout"], "device": ["mobile"]} reads as
    "reached checkout on mobile, bought or not".
    """
    if not isinstance(spec, dict):
        return {}
    out = {}

    def _multi(key, allowed):
        raw = spec.get(key)
        if raw is None or raw == "":
            return
        values = raw if isinstance(raw, (list, tuple, set)) else [raw]
        clean = []
        for value in values:
            text = str(value or "").strip().lower()
            # "all" is the legacy spelling of "everyone who did not purchase".
            if key == "outcome" and text == "all":
                for legacy in ("checkout", "rest"):
                    if legacy not in clean:
                        clean.append(legacy)
                continue
            if text in allowed and text not in clean:
                clean.append(text)
        if clean and len(clean) < len(allowed):
            # Selecting every value is the same as no constraint at all.
            out[key] = sorted(clean)

    _multi("outcome", OUTCOME_VALUES)
    _multi("device", DEVICE_VALUES)

    for key in ("utm_source", "utm_medium", "utm_campaign", "utm_content"):
        value = spec.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip().lower()
    if "fbclid" in spec and spec["fbclid"] is not None:
        out["fbclid"] = bool(spec["fbclid"])
    return out


def slice_is_empty(spec: dict) -> bool:
    return not spec


def row_matches_slice(row: dict, spec: dict) -> bool:
    if not spec:
        return True
    want_outcome = spec.get("outcome")
    if want_outcome and (row.get("outcome") or "rest") not in want_outcome:
        return False
    want_device = spec.get("device")
    if want_device:
        # Unknown device can't satisfy a device constraint.
        if (row.get("device") or "") not in want_device:
            return False
    attrs = row.get("attribution") if isinstance(row.get("attribution"), dict) else {}
    if "fbclid" in spec:
        has = bool(attrs.get("fbclid"))
        if spec["fbclid"] and not has:
            return False
        if not spec["fbclid"] and has:
            return False
    for key in ("utm_medium", "utm_campaign", "utm_content"):
        want = spec.get(key)
        if not want:
            continue
        got = str(attrs.get(key) or "").strip().lower()
        if got != want:
            return False
    want_source = spec.get("utm_source")
    if want_source:
        if want_source == "meta":
            if not is_meta_attribution(attrs):
                return False
        elif want_source in ("google", "non_meta"):
            if want_source == "non_meta":
                if is_meta_attribution(attrs):
                    return False
            else:
                got = str(attrs.get("utm_source") or "").strip().lower()
                if got != want_source:
                    return False
        else:
            got = str(attrs.get("utm_source") or "").strip().lower()
            if got != want_source:
                return False
    return True


OUTCOME_LABELS = {
    "purchased": "purchased",
    "checkout": "reached checkout",
    "rest": "did not reach checkout",
}


def slice_label(spec: dict) -> str:
    if not spec:
        return "everyone else"
    parts = []

    outcome = spec.get("outcome") or []
    if outcome:
        # The common "bought or not, but got to checkout" pair reads badly as
        # a raw join, so name the union directly.
        if set(outcome) == {"purchased", "checkout"}:
            parts.append("reached checkout")
        elif set(outcome) == {"checkout", "rest"}:
            parts.append("did not purchase")
        else:
            parts.append(" or ".join(OUTCOME_LABELS[o] for o in outcome))

    device = spec.get("device") or []
    if device:
        parts.append(" or ".join(device))

    if spec.get("utm_source") == "meta" or spec.get("fbclid") is True:
        parts.append("Meta")
    elif spec.get("utm_source") == "non_meta":
        parts.append("non-Meta")
    elif spec.get("utm_source"):
        parts.append(spec["utm_source"])
    if spec.get("fbclid") is False and spec.get("utm_source") != "non_meta":
        parts.append("no fbclid")
    for key, label in (
        ("utm_medium", "medium"),
        ("utm_campaign", "campaign"),
        ("utm_content", "content"),
    ):
        if spec.get(key):
            parts.append(f"{label}={spec[key]}")
    if not parts:
        return "all sessions"
    return " · ".join(parts)


CUT_PRESETS = {
    "purchased_vs_all": ({"outcome": ["purchased"]}, {"outcome": ["checkout", "rest"]}),
    "purchased_vs_checkout": ({"outcome": ["purchased"]}, {"outcome": ["checkout"]}),
    "checkout_vs_rest": ({"outcome": ["checkout"]}, {"outcome": ["rest"]}),
    "mobile_vs_desktop": ({"device": ["mobile"]}, {"device": ["desktop"]}),
    "meta_vs_rest": ({"utm_source": "meta"}, None),
    "meta_purchased_vs_rest": ({"outcome": ["purchased"], "utm_source": "meta"}, None),
    "google_vs_meta": ({"utm_source": "google"}, {"utm_source": "meta"}),
    "google_purchased_vs_rest": ({"outcome": ["purchased"], "utm_source": "google"}, None),
}


def cut_key(slice_a: Optional[dict], slice_b: Optional[dict]) -> str:
    """Stable cache key for a cut. Same two slices always hash the same way."""
    spec = {
        "a": normalize_slice(slice_a),
        "b": normalize_slice(slice_b),
    }
    return json.dumps(spec, sort_keys=True, separators=(",", ":"))


def split_rows_by_cut(rows: list, slice_a: Optional[dict], slice_b: Optional[dict]) -> tuple:
    """(rows_a, rows_b, spec_a, spec_b). An empty B means 'everyone not in A'."""
    spec_a = normalize_slice(slice_a)
    spec_b = normalize_slice(slice_b)
    if slice_is_empty(spec_a) and slice_is_empty(spec_b):
        spec_a = {"outcome": ["purchased"]}
        spec_b = {"outcome": ["checkout", "rest"]}
        complement = False
    else:
        complement = slice_is_empty(spec_b)

    rows_a, rows_b = [], []
    for row in rows:
        in_a = row_matches_slice(row, spec_a)
        in_b = (not in_a) if complement else row_matches_slice(row, spec_b)
        # A session matching both named slices counts in A only.
        if in_a:
            rows_a.append(row)
        elif in_b:
            rows_b.append(row)
    return rows_a, rows_b, spec_a, spec_b


CUT_PROMPT = """You compare two groups of recorded web sessions for a product team.

You receive per-session write-ups, each already grounded in that session's
replay. Group A and Group B are labelled. Work only from what the write-ups
say — never invent a page, a click, or an outcome that is not written there.

Your job is to find patterns ACROSS sessions, not to re-summarise each one.

Return a single JSON object with this exact shape:
{
  "headline": "one sentence on the sharpest difference between the groups",
  "shared": ["behaviour both groups show, with session counts"],
  "a_distinctive": ["what shows up in A and not (or far less) in B"],
  "b_distinctive": ["what shows up in B and not (or far less) in A"],
  "caveats": ["what these sessions cannot tell you"]
}

Rules that matter:
- Cite how many sessions back each observation, e.g. "3 of 5 A sessions".
  Count only sessions whose write-up actually supports the point.
- With fewer than 3 sessions on a side, say the evidence is thin rather than
  asserting a pattern. Small samples are the normal case here.
- A difference you can see in 1 session is an anecdote — put it in caveats,
  not in a_distinctive or b_distinctive.
- The write-ups describe what was on screen, not where anyone looked. Keep
  that distinction: say "reached" or "was shown", never "read" or "noticed".
- Prefer 2-4 items per list. Empty lists are fine when there is nothing solid.
- No preamble, no markdown — JSON only."""


def _cut_side_text(rows: list, label: str) -> str:
    """One side's write-ups, labelled per session so the model can cite counts."""
    if not rows:
        return f"### {label}\n(no sessions in this group)\n"
    parts = [f"### {label} — {len(rows)} session(s)\n"]
    for i, row in enumerate(rows, 1):
        outcome = "purchased" if row.get("outcome") == "purchased" else "did not purchase"
        attrs = row.get("attribution") if isinstance(row.get("attribution"), dict) else {}
        bits = [f"outcome: {outcome}"]
        for key in ("utm_source", "utm_campaign"):
            if attrs.get(key):
                bits.append(f"{key}: {attrs[key]}")
        if attrs.get("fbclid"):
            bits.append("meta click id present")
        text = (row.get("text") or "").strip() or "(no write-up)"
        parts.append(f"--- {label} session {i} ({'; '.join(bits)}) ---\n{text}\n")
    return "\n".join(parts)


def empty_cut_analysis(error: Optional[str] = None) -> dict:
    out = {
        "headline": "",
        "shared": [],
        "a_distinctive": [],
        "b_distinctive": [],
        "caveats": [],
    }
    if error:
        out["error"] = error
    return out


def normalize_cut_analysis(raw) -> dict:
    """Coerce the model's JSON into the documented shape, dropping anything else."""
    data = raw if isinstance(raw, dict) else {}
    out = empty_cut_analysis()
    headline = data.get("headline")
    if isinstance(headline, str):
        out["headline"] = headline.strip()[:400]
    for key in ("shared", "a_distinctive", "b_distinctive", "caveats"):
        items = data.get(key)
        if not isinstance(items, list):
            continue
        clean = []
        for item in items:
            if isinstance(item, str) and item.strip():
                clean.append(item.strip()[:500])
            if len(clean) >= 8:
                break
        out[key] = clean
    return out


async def call_gemini_cut(rows_a: list, rows_b: list, label_a: str, label_b: str) -> dict:
    """One text-only pass over both sides' write-ups."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    parts = [
        types.Part.from_text(
            text=(
                f"Group A is: {label_a}\nGroup B is: {label_b}\n\n"
                "The write-ups for both groups follow."
            )
        ),
        types.Part.from_text(text=_cut_side_text(rows_a, "GROUP A")),
        types.Part.from_text(text=_cut_side_text(rows_b, "GROUP B")),
    ]
    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            system_instruction=CUT_PROMPT,
            max_output_tokens=2048,
            thinking_config=types.ThinkingConfig(thinking_budget=512),
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    return normalize_cut_analysis(_parse_json_object(response.text or ""))


async def build_cut_analysis(
    rows: list, slice_a: Optional[dict], slice_b: Optional[dict]
) -> dict:
    """Split the cohort by a cut and ask the model what differs across sides."""
    if not ANALYSIS_ENABLED:
        raise RuntimeError("analysis is not configured — set GEMINI_API_KEY")

    rows_a, rows_b, spec_a, spec_b = split_rows_by_cut(rows, slice_a, slice_b)
    label_a = slice_label(spec_a)
    label_b = slice_label(spec_b) if spec_b else f"everyone not in {label_a}"

    base = {
        "cut": cut_key(slice_a, slice_b),
        "label_a": label_a,
        "label_b": label_b,
        "n_a": len(rows_a),
        "n_b": len(rows_b),
        "session_ids_a": [r.get("session_id") for r in rows_a],
        "session_ids_b": [r.get("session_id") for r in rows_b],
        "model": GEMINI_MODEL,
        "created_at": int(time.time() * 1000),
    }

    # Nothing to contrast — say so rather than spending a call on one side.
    if not rows_a or not rows_b:
        empty = empty_cut_analysis()
        which = label_a if not rows_a else label_b
        empty["headline"] = f"No analyzed sessions in {which} for this cohort."
        return {**base, "analysis": empty}

    _, model_gate = _gates()
    async with model_gate:
        analysis = await call_gemini_cut(rows_a, rows_b, label_a, label_b)
    return {**base, "analysis": analysis}


def sample_cohort_rows(
    conn: sqlite3.Connection,
    pool: str,
    purchased_n: int,
    all_n: int,
) -> list:
    """Newest purchased + newest non-purchased sessions that have at least one chunk.

    Prefers sessions with recorded clicks when breaking ties on recency, so thin
    bounce recordings are less likely to fill the sample.
    """
    activity = "coalesce(json_extract(interactions, '$.clicks'), 0)"
    purchased = conn.execute(
        f"""
        SELECT session_id FROM sessions
        WHERE pool = ?
          AND chunk_count > 0
          AND json_extract("order", '$.purchased_at') IS NOT NULL
        ORDER BY last_seen DESC, {activity} DESC, session_id DESC
        LIMIT ?
        """,
        (pool, purchased_n),
    ).fetchall()
    purchased_ids = {row[0] for row in purchased}

    all_rows = conn.execute(
        f"""
        SELECT session_id FROM sessions
        WHERE pool = ?
          AND chunk_count > 0
          AND json_extract("order", '$.purchased_at') IS NULL
        ORDER BY last_seen DESC, {activity} DESC, session_id DESC
        LIMIT ?
        """,
        (pool, all_n + len(purchased_ids)),
    ).fetchall()

    sessions = [{"session_id": sid, "filter": "purchased", "status": "queued"} for (sid,) in purchased]
    for (sid,) in all_rows:
        if sid in purchased_ids:
            continue
        sessions.append({"session_id": sid, "filter": "all", "status": "queued"})
        if sum(1 for s in sessions if s["filter"] == "all") >= all_n:
            break
    return sessions


def build_cohort(
    conn: sqlite3.Connection,
    pool_key: str,
    purchased_n: int = 5,
    all_n: int = 5,
) -> dict:
    now = int(time.time() * 1000)
    sessions = sample_cohort_rows(conn, pool_key, purchased_n, all_n)
    stamp = time.strftime("%b %-d, %H:%M", time.localtime(now / 1000))
    return {
        "id": f"cohort-{now}",
        # Auto label; renameable once the user decides a cohort is worth keeping.
        "label": f"Sample {purchased_n}+{all_n} · {stamp}",
        "created_at": now,
        "updated_at": now,
        "status": "queued",
        "purchased_n": purchased_n,
        "all_n": all_n,
        "sessions": sessions,
    }


def rename_cohort(pool: Path, cohort_id: str, label: str) -> Optional[dict]:
    """Set a cohort's display label. Membership is untouched."""
    cohort = read_cohort(pool, cohort_id)
    if not cohort:
        return None
    clean = str(label or "").strip()[:120]
    if not clean:
        raise ValueError("label cannot be empty")
    return write_cohort(pool, {**cohort, "label": clean})


async def run_extractor(session_dir: Path, out_dir: Path, chunk_names: list) -> dict:
    if not shutil.which("node"):
        raise RuntimeError("node is not installed on this server — analysis is unavailable")
    if not REPLAYER_BUNDLE.is_file():
        raise RuntimeError("replayer bundle missing — run `npm run build`")

    proc = await asyncio.create_subprocess_exec(
        "node",
        str(EXTRACTOR),
        str(session_dir),
        str(out_dir),
        str(REPLAYER_BUNDLE),
        # Positional, and ahead of the variadic chunk names.
        str(ANALYZE_FPS),
        str(ANALYZE_MAX_FRAMES),
        *chunk_names,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(BASE_DIR),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), ANALYZE_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("page reconstruction timed out")

    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"page reconstruction failed: {detail[-1] if detail else 'unknown error'}")

    try:
        return json.loads(stdout.decode("utf-8"))
    except Exception:
        raise RuntimeError("page reconstruction returned no digest")


def _seconds(ms) -> float:
    return round((ms or 0) / 1000, 1)


def _clock(ms) -> str:
    """MM:SS, the same vocabulary the frame labels use."""
    total = int((ms or 0) // 1000)
    return f"{total // 60:02d}:{total % 60:02d}"


def prompt_digest(digest: dict) -> dict:
    """Trim the digest to what the model needs, in seconds rather than ms."""
    session = digest.get("session") or {}
    frames = digest.get("frames") or []
    return {
        "session": {
            "duration_s": _seconds(session.get("durationMs")),
            "active_s": _seconds(session.get("activeMs")),
            "idle_s": _seconds(session.get("idleMs")),
            "viewport": session.get("viewport"),
            "page_height_px": session.get("pageHeight"),
            "max_scroll_px": session.get("maxScrollPx"),
            "pages": session.get("urls") or [],
        },
        "clicks_in_order": [
            {
                "at": _clock(c.get("atMs")),
                "at_s": _seconds(c.get("atMs")),
                "label": c.get("text") or None,
                "element": c.get("tag") or None,
                "selector": c.get("selector") or None,
                "image_alt": (c.get("image") or {}).get("alt"),
                "rage_click": c.get("rage", False),
            }
            for c in (digest.get("clicks") or [])
        ],
        "frames_at": [_clock(f.get("atMs")) for f in frames],
        "warnings": digest.get("warnings") or [],
    }


async def call_gemini(digest: dict, frames: list) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)

    usable = [f for f in frames if Path(f["file"]).is_file()]

    parts = [
        types.Part.from_text(
            text="Measured digest for this session:\n\n"
            + json.dumps(prompt_digest(digest), indent=2)
        ),
        types.Part.from_text(
            text=f"\n{len(usable)} frames follow in chronological order, sampled "
            "through the active parts of the session. Each is labelled with its "
            "timestamp in the recording."
        ),
    ]
    # Label before the image: the model attributes a caption to what follows it
    # far more reliably than to what precedes it.
    for frame in usable:
        parts.append(types.Part.from_text(text=f"[{_clock(frame['atMs'])}]"))
        parts.append(
            types.Part.from_bytes(
                data=Path(frame["file"]).read_bytes(), mime_type="image/jpeg"
            )
        )

    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            # max_output_tokens covers thinking as well as the answer, and a
            # long frame sequence makes the model think hard: at 1400 with no
            # budget, thinking ate all but ~50 tokens and the analysis came
            # back cut off mid-sentence.
            max_output_tokens=4000,
            thinking_config=types.ThinkingConfig(thinking_budget=1024),
            temperature=0.3,
        ),
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty analysis")
    return text


def _parse_json_object(text: str) -> dict:
    """Parse a JSON object, tolerating optional markdown fences."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty model response")
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        raw = fence.group(1).strip()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("model response was not an object")
    return data


async def analyze_session(session_dir: Path, chunk_names: list, force: bool = False) -> dict:
    if not ANALYSIS_ENABLED:
        raise RuntimeError("analysis is not configured — set GEMINI_API_KEY")

    if not force:
        cached = read_analysis(session_dir)
        if cached:
            return cached

    analysis_path, out_dir = analysis_paths(session_dir)
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    extract_gate, model_gate = _gates()

    async with extract_gate:
        digest = await run_extractor(session_dir, out_dir, chunk_names)
    if digest.get("error"):
        raise RuntimeError(digest["error"])

    async with model_gate:
        text = await call_gemini(digest, digest.get("frames") or [])

    result = {
        "status": "done",
        "text": text,
        "model": GEMINI_MODEL,
        "created_at": int(time.time() * 1000),
        "frame_count": len(digest.get("frames") or []),
        "warnings": digest.get("warnings") or [],
    }
    analysis_path.write_text(json.dumps(result))
    clear_job(session_dir)
    return result


def _cli_data_root() -> Path:
    return Path(os.environ.get("CAPTURE_DATA_DIR") or BASE_DIR / "data")


def _cli_pool(project: Optional[str]) -> Path:
    root = _cli_data_root()
    if not project:
        return root / "sessions"
    return root / "projects" / project / "sessions"


async def _cli_run_cohort(pool: Path, pool_key: str, purchased_n: int, all_n: int, force: bool) -> int:
    index = _cli_data_root() / "index.db"
    if not index.is_file():
        print(f"no session index at {index} — start the server once to build it")
        return 1
    if not ANALYSIS_ENABLED:
        print("analysis is not configured — set GEMINI_API_KEY")
        return 1
    if not runtime_present():
        print("analysis runtime unavailable — need node, extract.mjs, and the replayer bundle")
        return 1

    conn = sqlite3.connect(index)
    try:
        cohort = build_cohort(conn, pool_key, purchased_n, all_n)
    finally:
        conn.close()

    if not cohort["sessions"]:
        print("no sessions matched the purchased/all sample")
        write_cohort(pool, cohort)
        return 1

    write_cohort(pool, {**cohort, "status": "running"})
    print(
        f"{cohort['label']}: {len(cohort['sessions'])} session(s) → "
        f"{cohort_path(pool, cohort['id'])}"
    )

    for entry in cohort["sessions"]:
        session_dir = pool / entry["session_id"]
        chunk_names = sorted(
            (c.name for c in session_dir.iterdir() if c.is_file() and SAFE_CHUNK.match(c.name))
        )
        if not chunk_names:
            entry["status"] = "error"
            entry["error"] = "no chunks to analyze"
            write_cohort(pool, cohort)
            print(f"  {entry['filter']:9} {entry['session_id'][:8]}… error (no chunks)")
            continue

        entry["status"] = "running"
        write_cohort(pool, cohort)
        print(f"  {entry['filter']:9} {entry['session_id'][:8]}… running", flush=True)
        try:
            if force:
                clear_analysis(session_dir)
            await analyze_session(session_dir, chunk_names, force=force)
            entry["status"] = "done"
            entry["analysis"] = ANALYSIS_FILE
            entry.pop("error", None)
            print(f"  {entry['filter']:9} {entry['session_id'][:8]}… done")
        except Exception as exc:
            entry["status"] = "error"
            entry["error"] = str(exc)
            write_job(session_dir, "error", error=str(exc))
            print(f"  {entry['filter']:9} {entry['session_id'][:8]}… error ({exc})")
        write_cohort(pool, cohort)

    final = enrich_cohort_status(pool, cohort)
    write_cohort(pool, final)
    prog = final["progress"]
    print(
        f"{final['status']}: {prog['done']} done · {prog['error']} error · "
        f"{prog['queued']} queued of {prog['total']}"
    )
    return 0 if prog["error"] == 0 and prog["done"] == prog["total"] else 1


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Capture session analysis")
    sub = parser.add_subparsers(dest="cmd")

    cohort_p = sub.add_parser("cohort", help="Sample purchased+all sessions and analyze them")
    cohort_p.add_argument("--purchased", type=int, default=5, help="purchased sample size (default 5)")
    cohort_p.add_argument("--all", type=int, default=5, dest="all_n", help="all/non-purchase sample size (default 5)")
    cohort_p.add_argument("--project", default="", help="project token (default: no-project pool)")
    cohort_p.add_argument("--force", action="store_true", help="re-analyze even when analysis.json exists")

    args = parser.parse_args(argv)
    if args.cmd == "cohort":
        if args.purchased < 0 or args.all_n < 0:
            print("sample sizes must be >= 0")
            return 1
        pool = _cli_pool(args.project or None)
        pool_key = "" if not args.project else args.project
        return asyncio.run(
            _cli_run_cohort(pool, pool_key, args.purchased, args.all_n, args.force)
        )

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Session analysis: replay the session headlessly, then ask Gemini about it.

The extractor drives the same CapturePlayer the replay UI uses, so animations
are frozen and recorded scroll offsets are re-applied exactly as they are on
screen. Stepping that player yields a frame per second of activity — the page
as the visitor saw it change, rather than a single end-state snapshot.

Also owns cohorts: sample latest N purchased + N non-purchase sessions (default
50+50, ashwamag-01, ≥5s), persist each sample under `cohorts/<id>.json` without
touching prior cohorts, reconstruct pages locally, then run write-ups through
the Gemini Batch API (interactive generate_content remains for single-session
Analyze). A cohort is immutable once sampled, so a "cut" across it can be
analysed once and cached beside it (`refresh=1` busts that cache).
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import sqlite3
import time
import urllib.parse
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

# Insights cohort defaults: latest ashwamag-01 sessions ≥5s, 50/50 split.
COHORT_DEFAULT_PURCHASED_N = 50
COHORT_DEFAULT_ALL_N = 50
COHORT_MIN_DURATION_MS = 5000
COHORT_URL_CONTAINS = "/pages/ashwamag-01"

# Cohort write-ups go through Gemini Batch (≈50% cheaper). Single-session
# Analyze stays on the interactive API. Set ANALYZE_BATCH=0 to force sync.
ANALYZE_BATCH = os.environ.get("ANALYZE_BATCH", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)
ANALYZE_BATCH_POLL_S = float(os.environ.get("ANALYZE_BATCH_POLL_S", "15"))
# Inline batch payloads cap around 20MB; above this we upload a JSONL file.
ANALYZE_BATCH_INLINE_MAX_BYTES = int(
    os.environ.get("ANALYZE_BATCH_INLINE_MAX_BYTES", str(15 * 1024 * 1024))
)

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

SYSTEM_PROMPT = """You are a senior consumer behavior and marketing analyst analyzing one recorded website session for a consumer brand.

You receive:
•⁠  ⁠session metadata such as source/campaign, new vs. returning, device, geography, purchase outcome, order value, and products purchased
•⁠  ⁠a measured session digest
•⁠  ⁠chronological replay frames labelled [MM:SS]

Your job is not just to describe what happened. Reconstruct the visitor’s likely decision-making process and infer what their behavior reveals about them as a consumer.

Be aggressive but evidence-based in making hypotheses about:
•⁠  ⁠purchase intent and shopping mode
•⁠  ⁠what they care about most
•⁠  ⁠motivations and decision criteria
•⁠  ⁠objections, anxieties, or unanswered questions
•⁠  ⁠price sensitivity, trust, and need for social proof
•⁠  ⁠why intent strengthened or weakened
•⁠  ⁠likely reason for purchase or abandonment

Look at sequences of behavior, not isolated actions. Ask what the visitor appears to be trying to learn, validate, compare, or resolve.

Clearly distinguish observed behavior from interpretation. Use language like “likely,” “appears,” and “suggests” for inferred motivations. Do not infer sensitive personal characteristics.

Ground claims in the frames, digest, and metadata. Never invent actions, elements, numbers, or information not shown. Frames prove what was on screen, not what the visitor actually read or looked at. A long pause may indicate consideration, distraction, or inactivity, so interpret it using surrounding behavior.

Write these sections:

## Consumer read
2 to 4 sentences describing who this shopper appears to be, their intent, what matters to them, their main hesitation, and what likely drove the outcome.

## Decision journey
Explain the key moments in how their decision process evolved, with [MM:SS] citations. Focus on moments where intent strengthened, weakened, or a new question emerged. Skip insignificant activity.

## What they care about
Identify the 2 to 4 strongest inferred purchase drivers, questions, or objections and explain the behavioral evidence for each.

## Conversion diagnosis
Explain the most likely reason they purchased or did not purchase. If several explanations are plausible, rank them. Distinguish actual friction from simple low intent.

## Marketing implications
Give 2 to 4 specific implications for messaging, PDPs, offers, merchandising, personalization, acquisition, retargeting, or lifecycle marketing. Avoid generic CRO advice. Focus on insights that would matter if the same pattern appears across many sessions.

## Friction
Briefly identify concrete UX issues such as failed clicks, rage clicks, confusion, backtracking, or technical problems. Do not mistake normal consideration for friction.

A checkout screen that sits unchanged for a long time is NOT a technical bug. Shopify hosts checkout outside the storefront, so recording stops there and the last frames often freeze on checkout while the visitor continues on Shopify's page. Do not flag that freeze, a "stuck" checkout UI, or a long idle at checkout as a site failure or tech issue.

End with:

Shopper type: [description] | Intent: [low/medium/high] | Primary driver: [driver] | Main barrier: [barrier] | Outcome: [short explanation]

Keep the analysis concise, opinionated, and inference-heavy. Prefer explaining why the visitor behaved this way over recounting everything they did. """

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
    """Cache one cut. A done cohort is immutable, so this only needs busting
    when the question changes (a reworded prompt), never when the data does."""
    path = cuts_path(pool, cohort_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    cuts = read_cuts(pool, cohort_id)
    cuts[key] = cut
    path.write_text(json.dumps(cuts, indent=2))
    return cut


def clear_cut(pool: Path, cohort_id: str, key: str) -> bool:
    """Drop one cached cut so the next run re-asks the model. Returns whether
    anything was actually dropped."""
    try:
        path = cuts_path(pool, cohort_id)
    except ValueError:
        return False
    cuts = read_cuts(pool, cohort_id)
    if key not in cuts:
        return False
    cuts.pop(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cuts, indent=2))
    return True


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

    status = cohort.get("status") or "idle"
    if status not in ("idle", "queued", "running", "paused", "done", "error"):
        status = "idle"

    if done >= len(sessions) and sessions:
        status = "done"
    elif not sessions:
        status = "done"
    elif status == "idle":
        # Stay sampled-until-started unless work is somehow already in flight.
        if running:
            status = "running"
    elif status == "paused":
        # Keep paused until the user hits Start again (even if jobs look stale).
        if running:
            status = "running"
    elif running or (queued and status in ("running", "queued")):
        status = "running"
    elif errors and done + errors >= len(sessions) and not queued and not running:
        status = "error" if done == 0 else "done"

    selection = cohort.get("selection")
    if not selection:
        selection = cohort_selection_text(
            int(cohort.get("purchased_n") or 0),
            int(cohort.get("all_n") or 0),
            min_duration_ms=int(
                cohort.get("min_duration_ms")
                if cohort.get("min_duration_ms") is not None
                else COHORT_MIN_DURATION_MS
            ),
            url_contains=str(cohort.get("url_contains") or ""),
        )

    return {
        **cohort,
        "selection": selection,
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


def page_path(url: Optional[str]) -> Optional[str]:
    """Landing-page path from an entry URL: pathname only, no query or hash.

    Trailing slashes are stripped (except `/`) so `/p` and `/p/` share a facet.
    Query strings stay on attribution — putting them here would explode the
    picker with one option per UTM combo.
    """
    raw = str(url or "").strip()
    if not raw:
        return None
    try:
        if raw.startswith("/") and "://" not in raw:
            path = urllib.parse.unquote(raw.split("?", 1)[0].split("#", 1)[0])
        else:
            parsed = urllib.parse.urlparse(
                raw if "://" in raw else f"https://{raw}"
            )
            path = urllib.parse.unquote(parsed.path or "") or "/"
    except Exception:
        return None
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return path or None


def normalize_slice(spec: Optional[dict]) -> dict:
    """Normalize a slice: any combination of outcome, device, page and attribution.

    Keys: outcome (list of purchased|checkout|rest), device (list of
    mobile|desktop), page (landing path), utm_source, utm_medium, utm_campaign,
    utm_content, fbclid. utm_source="meta" matches Meta traffic (fbclid or a
    known Meta source).

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

    page = page_path(spec.get("page")) if spec.get("page") not in (None, "") else None
    if page:
        out["page"] = page

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
    want_page = spec.get("page")
    if want_page:
        # Unknown / missing landing page can't satisfy a page constraint.
        if (row.get("page") or "") != want_page:
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

    if spec.get("page"):
        parts.append(spec["page"])

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


def cut_key(slice_spec: Optional[dict]) -> str:
    """Stable cache key for a cut. The same filter always hashes the same way."""
    return json.dumps(
        {"filter": normalize_slice(slice_spec)}, sort_keys=True, separators=(",", ":")
    )


def select_rows_by_cut(rows: list, slice_spec: Optional[dict]) -> tuple:
    """(matching_rows, spec). An empty filter selects the whole cohort."""
    spec = normalize_slice(slice_spec)
    return [row for row in rows if row_matches_slice(row, spec)], spec


CUT_PROMPT = """You turn recorded web sessions into insights a MARKETING team can
act on: what these visitors responded to, where they lost interest, which
messaging and offers carried weight, and what the team should do next.

You receive per-session write-ups, each already grounded in that session's
replay. Work only from what the write-ups say — never invent a page, a click,
a number, or an outcome that is not written there.

## Write for a marketer, not an engineer

Your reader plans campaigns, landing pages, offers and creative. They do not
read replays and will not act on a raw log of clicks and timestamps. Every
item must answer "so what?" — what does this tell us about these customers,
and what should we do about it?

  BAD   "Clicked the supply selector at [01:12] and chose '30-Night Supply'."
  GOOD  "44% (4 of 9) switched off the default '90-Night Supply' to the
         cheapest option after ~40s on the guarantee — the 90-night default
         may be anchoring too high for first-time buyers.
         Action: test '30-Night' as the default, or lead with the guarantee
         earlier to justify the larger pack."

  BAD   "Sessions from mobile scrolled to the ingredient breakdown."
  GOOD  "Mobile visitors spent 25-60s on the ingredient breakdown, roughly
         twice as long as desktop — the scientific detail is doing real work
         for this segment.
         Action: keep the ingredient depth on mobile and consider leading
         Meta creative with the science rather than the discount."

Lead with the customer behaviour and its share, then the reading, then the
action. Timestamps and quoted labels are still required as EVIDENCE inside
the observation — they prove the claim — but they are never the point of it.

## Use percentages, not just counts

Marketers think in shares. Give the percentage first with the raw count in
brackets: "44% (4 of 9)". Round to whole numbers. With fewer than 5 sessions
a percentage overstates precision, so use "4 of 9 sessions" alone and say the
evidence is directional.

## The single most important rule

The group was SELECTED by a filter, stated below as "This group is".
Everything entailed by that filter is already known and is NOT a finding.

Before you write any item, apply this test:

  "Could I have predicted this from the group's definition alone,
   without reading a single write-up?"

If yes, DELETE IT. It is not an observation, it is the filter talking.

Worked example. If the group is "purchased", every one of these is banned:
  BANNED  "clicked 'Buy now' or 'Add to cart' (6 of 7)"
  BANNED  "proceeded to checkout"
  BANNED  "completed a purchase"
  BANNED  "actively interacted with purchase options"
  BANNED  "showed strong purchase intent"
Buyers buy. That is what makes them buyers. Reporting it tells the team nothing.

The same test kills "none of them clicked a CTA" when the group is defined as
not converting, and "sessions came from campaign X" when X is the filter.

What IS a finding, for the same group:
  KEEP  "57% (4 of 7) switched off the default '90-Night Supply' before
         buying — the default may be anchoring too high."
  KEEP  "Nobody reached 'Choose your supply' by scrolling; all 4 who got
         there were carried by the sticky CTA — the offer is effectively
         invisible without it."
  KEEP  "Clicked the cart icon at [00:17] and nothing happened (1 of 5) —
         a broken path to checkout."

The difference: a finding tells the team something they did not already know
about these customers, and points at a decision — an offer, a default, a
message, a placement, a channel, a broken step. If your item does not change
what someone would do next week, it is not worth writing.

Route unavoidable outcome facts into how they happened, never that they
happened.

## Be concrete, not abstract

The write-ups are specific. Your output must stay at that same resolution.
You are not summarising them — you are pulling the specific, checkable
evidence out of them and grouping it.

Bad (abstract, unusable):  "Visitors engaged extensively with detailed
                            product information."
Good (concrete, actionable): "Scrolled past the ingredient breakdown without
                            stopping, then stalled 40s on the '30 nights or
                            every penny back' guarantee (3 of 5)."

In every item, wherever the write-ups give it, include:
- what was actually on screen — quote section headings, button labels,
  product names, and price or offer wording verbatim from the write-up
- where in the page it happened — scroll depth in px, or "near the FAQ",
  "above the fold", "the third testimonial"
- how long it took — timestamps [MM:SS] or durations
- what the click did, especially when it did nothing or did the wrong thing

Numbers and quoted labels are what makes an item worth reading. An item with
none of them is almost certainly too vague to keep.

You are reading several write-ups at once, so you can see what one session
alone cannot: the same stall on the same section, the same dead click, the
same abandoned step. Name those specifically. Resist the pull toward a tidy
summary — the team already knows the shape of their funnel, and a sentence
that would be true of any e-commerce site tells them nothing.

## Look for clusters, not a single average session

A group is rarely uniform. The sessions in it usually fall into two or three
distinct behaviours, and those splits are the most useful thing you can find.
Look for them deliberately before you write anything.

Never report "no common behaviour" or "the sessions varied" and stop there.
That is the finding — keep going and say HOW they split, and what separates
the groups. A split you can name beats a similarity you had to stretch for.

Typical splits worth naming:
- Some reached a section and others never got there
- Some used one route in (sticky CTA, nav, search) and others another
- Some stalled where others moved straight through
- Some hit a broken interaction and others never touched it
- Some moved fast and decisively, others looped back repeatedly

Write a split as ONE item that names both sides with counts and evidence:

  GOOD  "Split on how they reached 'Choose your supply': 4 arrived via the
         sticky CTA at [00:31]-[00:44] and bought; the other 5 scrolled past
         it entirely and none reached checkout."
  GOOD  "3 sessions rage-clicked the cart icon at ~[00:17] and abandoned; the
         other 6 never touched it and moved to checkout in under 90s."
  BAD   "Behaviour varied across sessions."
  BAD   "Some users engaged more than others."

A cluster of 3 in a group of 9 is a real pattern, not an anecdote — report it
as a split with its count, not as a caveat. The one-session rule below applies
to genuinely isolated sessions, not to a minority that behaves consistently.

### Split on the session labels too

Every session below is labelled with its outcome, device, landing page, traffic
source and campaign. Test each as a candidate split — these are the ones the
team can act on directly. (Any dimension named under "Already known" is the
same for every session here and cannot split anything — skip it and use the
rest.)
- outcome (purchased / reached checkout / did not reach checkout): what did
  the sessions that converted do differently on the page from those that did
  not? This is the most valuable split in the set.
- device (mobile / desktop): the same page often works very differently on
  each, and mobile and desktop usually deserve different creative.
- page (landing path, e.g. /products/cream): visitors who landed on different
  URLs arrived with different context. A page whose visitors behave differently
  is a landing-page or merchandising decision.
- utm_source and utm_campaign: visitors from different sources land with
  different expectations. A source whose visitors behave differently is a
  targeting or creative decision, and one of the most actionable splits here.

State the behavioural difference, never the label itself. The labels are given
to you — that the purchasers purchased, or that the mobile sessions were on
mobile, is not a finding (see the rule above). What they DID differently is.

  GOOD  "All 4 purchasers switched off the default '90-Night Supply'; the 5
         who did not buy left it untouched — the pack size, not the product,
         looks like the deciding factor."
  BAD   "Purchased sessions completed a purchase, unlike the others."
  GOOD  "Meta visitors went straight to price and left within 40s; Google
         visitors read through the ingredients first — the two channels are
         arriving with different questions."
  BAD   "Mobile and desktop sessions behaved differently."

When a metadata split shows no behavioural difference, say that plainly — "the
mobile and desktop sessions did the same things" is a real, useful result.

When the group really is uniform, say so plainly and move on — but only after
looking. "They all did X (9 of 9)" is a strong finding when it is true.

## Output

Return a single JSON object with this exact shape. Every item in journey /
attention / intent / blockers is an OBJECT with three fields — never a bare string.
"recommendations" and "caveats" are plain string lists.
{
  "headline": "one sentence a marketer could put at the top of a deck: the sharpest finding, with its share and what it means",
  "journey": [
    {
      "observation": "what these visitors did, with the evidence — quoted labels, [MM:SS], durations — and one hedged reading of what it suggests",
      "share": "44% (4 of 9)",
      "action": "what the team should do about it: an offer, a message, a placement, a channel, a test"
    }
  ],
  "attention": [{"observation": "...", "share": "...", "action": "..."}],
  "intent": [{"observation": "...", "share": "...", "action": "..."}],
  "blockers": [{"observation": "...", "share": "...", "action": "..."}],
  "caveats": ["plain strings here — what these sessions cannot tell you, and any single-session oddity worth a look"],
  "recommendations": [
    "concrete next steps drawn from the findings above — offer, message, placement, channel, or test. 3 to 6 items. Shown last in the UI."
  ]
}

What each section is for:
- "journey" — how they moved through the site and where they dropped off.
  Which entry points and routes worked, where each cluster stopped, and what
  that says about the landing experience.
- "attention" — what held them and what lost them. Which sections, claims or
  offers they sat on, and for how long. This is where messaging and creative
  insight lives: a 40s stall on a guarantee tells you what is doing the
  selling.
- "intent" — the moments that show buying intent or hesitation: the options
  they compared, the defaults they changed, the controls they used, what they
  did just before converting or leaving.
- "blockers" — what stopped them: dead clicks, rage clicks, unexpected
  redirects, modals, repeated attempts, confusing steps. Every one a write-up
  mentions belongs here, because each is money left on the table. An empty
  blockers list must mean the write-ups describe none — not that you did not
  look.
- "caveats" — plain strings, no objects. Sample limits and one-off oddities.
- "recommendations" — the marketer's punch list, last. Plain strings only. Every
  item must be a concrete next step (test this default, move this proof
  point up, lead this channel with that message, fix this broken step).
  Pull them from the actions on the findings above; do not invent steps the
  evidence does not support. Deduplicate: one recommendation per decision.
  Never "investigate further" or "monitor this".

Writing the three fields:
- "observation" carries the evidence AND the reading. It must contain at least
  one concrete anchor from the write-ups — a quoted label or heading, an
  [MM:SS], a duration, or a scroll position — followed by what it suggests.
- "share" is the size of the pattern: "44% (4 of 9)". Below 5 sessions use
  "4 of 9 sessions" with no percentage. Use "9 of 9" for the whole group.
  For a split, describe the side this item is about.
- "action" is a concrete next step for a marketing team — test this default,
  move this proof point up, lead this channel with that message, fix this
  broken step. Never "investigate further" or "monitor this": if the evidence
  does not support a real recommendation, leave the action as an empty string
  and keep the observation.

Rules:
- Every item's "share" must reflect only the sessions whose write-up actually
  supports the point. Do not round a minority up into "most".
- With fewer than 3 sessions, say the evidence is thin in the observation
  rather than asserting a pattern. Small samples are the normal case here.
- A behaviour visible in only 1 session is an anecdote — put it in caveats
  rather than stating it as a group pattern. A specific, surprising anecdote
  is worth more there than a vague pattern would be anywhere else. Friction is
  the exception: a single dead click or rage click is worth reporting in
  friction on its own. This rule is about isolated sessions only — 2 or more
  sessions behaving the same way is a cluster and belongs in its section with
  a count, however small a share of the group it is.
- Keep observation and interpretation separate, and mark which is which.
  The write-ups describe what was on screen, not where anyone looked, so state
  the OBSERVED part in those terms: "reached", "was shown", "scrolled past",
  "sat 40s on" — never "read", "noticed", or "was interested in" as if it were
  fact. You may then INFER what it suggests, provided the inference is hedged
  and follows the evidence:
    OK      "Sat 40s on the ingredient breakdown (56%) — suggests the
             scientific detail is carrying weight for this segment."
    OK      "...may indicate the price is the sticking point, not the claim."
    NOT OK  "Users read the ingredients and were convinced by the science."
  One hedged reading per item is plenty. An item that is all inference and no
  evidence is worthless — the evidence is what makes the reading credible.
- Aim for 3-8 items in each section, as many as the write-ups genuinely
  support. Prefer more specific items over fewer general ones: two precise
  observations are worth more than one that merges them and loses the detail.
  Never merge two distinct observations into one item to save room.
- Every "observation" must carry at least one [MM:SS] timestamp, a quoted
  label or heading, a duration, or a scroll position. An observation with none
  of these is too vague to keep — cut it and write a sharper one.
- Two different items must not recommend the same action in different words.
  If two observations lead to one action, merge them into a single item.
- A section may be empty only when the write-ups genuinely say nothing about
  it. Empty because you ran out of room is not acceptable.
- No preamble, no markdown — JSON only."""


def _cut_side_text(rows: list, label: str) -> str:
    """The group's write-ups, labelled per session so the model can cite counts."""
    if not rows:
        return f"### {label}\n(no sessions in this group)\n"
    parts = [f"### {label} — {len(rows)} session(s)\n"]
    for i, row in enumerate(rows, 1):
        attrs = row.get("attribution") if isinstance(row.get("attribution"), dict) else {}
        # Every clusterable field is stated on every session, "unknown"
        # included: a missing label reads as a difference the model would
        # otherwise cluster on by accident.
        bits = [f"outcome: {OUTCOME_LABELS.get(row.get('outcome'), 'unknown')}"]
        bits.append(f"device: {row.get('device') or 'unknown'}")
        bits.append(f"page: {row.get('page') or 'unknown'}")
        for key in ("utm_source", "utm_campaign"):
            bits.append(f"{key}: {attrs.get(key) or 'none'}")
        if attrs.get("fbclid"):
            bits.append("meta click id present")
        text = (row.get("text") or "").strip() or "(no write-up)"
        parts.append(f"--- {label} session {i} ({'; '.join(bits)}) ---\n{text}\n")
    return "\n".join(parts)


def _given_facts(spec: dict) -> str:
    """Spell out what the filter already guarantees, so it is not 'found'."""
    lines = []

    if spec.get("outcome"):
        joined = " or ".join(OUTCOME_LABELS.get(v, v) for v in spec["outcome"])
        lines.append(
            f"- Every session in this group {joined}. Do not report this, or "
            f"any step it entails (clicking buy, reaching checkout, "
            f"converting), as a finding."
        )
    if spec.get("device"):
        lines.append(
            f"- Every session in this group was on "
            f"{' or '.join(spec['device'])}. Do not report the device itself "
            f"as a finding."
        )
    if spec.get("page"):
        lines.append(
            f"- Every session in this group landed on {spec['page']}. Do not "
            f"report the landing page itself as a finding."
        )
    for key in ("utm_source", "utm_medium", "utm_campaign", "utm_content"):
        if spec.get(key):
            lines.append(
                f"- Every session in this group came from {key}={spec[key]}. "
                f"Do not report the traffic source as a finding."
            )

    # A filtered field is constant here, so it cannot split the group. Point
    # the clustering at the dimensions that still vary.
    free = [
        name
        for name, key in (
            ("outcome", "outcome"),
            ("device", "device"),
            ("landing page", "page"),
            ("traffic source", "utm_source"),
            ("campaign", "utm_campaign"),
        )
        if not spec.get(key)
    ]
    if lines and free:
        lines.append(
            "- These are fixed for every session here, so they cannot separate "
            "one cluster from another. Cluster on " + ", ".join(free) + " and "
            "on what the sessions actually did on the page."
        )

    if not lines:
        return ""
    return "## Already known — never report these as findings\n\n" + "\n".join(lines)


# Insight sections hold {observation, share, action} objects; recommendations
# and caveats stay plain string lists (a punch list / sample limit has no share).
CUT_INSIGHT_SECTIONS = ("journey", "attention", "intent", "blockers")
CUT_SECTIONS = CUT_INSIGHT_SECTIONS + ("caveats",)
# The prompt asks for 3-8 items; keep a little headroom so a good extra item
# is kept rather than silently dropped.
CUT_ITEMS_PER_SECTION = 10
CUT_RECOMMENDATIONS_CAP = 8


def empty_cut_analysis(error: Optional[str] = None) -> dict:
    out = {
        "headline": "",
        "recommendations": [],
        **{key: [] for key in CUT_SECTIONS},
    }
    if error:
        out["error"] = error
    return out


def _clean_text(value, limit: int) -> str:
    """Trimmed string, or "" for anything that is not usable text."""
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else ""


def _insight_item(item) -> Optional[dict]:
    """One {observation, share, action}, or None when there is no observation.

    A bare string is accepted as the observation: the model occasionally falls
    back to the old flat shape, and an item with no action still carries the
    finding. Share and action are optional and render only when present.
    """
    if isinstance(item, str):
        observation = _clean_text(item, 900)
        return {"observation": observation, "share": "", "action": ""} if observation else None
    if not isinstance(item, dict):
        return None
    # Generous limits: a good observation quotes labels and cites timestamps,
    # so it runs long. Truncating it would strip exactly the useful part.
    observation = _clean_text(item.get("observation"), 900)
    if not observation:
        return None
    return {
        "observation": observation,
        "share": _clean_text(item.get("share"), 60),
        "action": _clean_text(item.get("action"), 400),
    }


def _recommendation_line(item) -> str:
    """One punch-list line: a bare string, or the action field of an object."""
    if isinstance(item, str):
        return _clean_text(item, 400)
    if isinstance(item, dict):
        for key in ("action", "recommendation", "text"):
            line = _clean_text(item.get(key), 400)
            if line:
                return line
    return ""


def _recommendations_from_actions(out: dict) -> list:
    """Fallback punch list: unique non-empty actions from the insight items.

    Older cached cuts predate the top-level field; deriving from actions keeps
    the Recommendations block populated without a re-ask.
    """
    seen = set()
    lines = []
    for key in CUT_INSIGHT_SECTIONS:
        for item in out.get(key) or []:
            if not isinstance(item, dict):
                continue
            line = _clean_text(item.get("action"), 400)
            if not line or line.lower() in seen:
                continue
            seen.add(line.lower())
            lines.append(line)
            if len(lines) >= CUT_RECOMMENDATIONS_CAP:
                return lines
    return lines


def normalize_cut_analysis(raw) -> dict:
    """Coerce the model's JSON into the documented shape, dropping anything else."""
    data = raw if isinstance(raw, dict) else {}
    out = empty_cut_analysis()
    out["headline"] = _clean_text(data.get("headline"), 600)
    for key in CUT_SECTIONS:
        items = data.get(key)
        if not isinstance(items, list):
            continue
        clean = []
        for item in items:
            # Caveats stay plain strings; the insight sections are objects.
            entry = _clean_text(item, 900) if key == "caveats" else _insight_item(item)
            if entry:
                clean.append(entry)
            if len(clean) >= CUT_ITEMS_PER_SECTION:
                break
        out[key] = clean
    recs = data.get("recommendations")
    if isinstance(recs, list):
        clean = []
        seen = set()
        for item in recs:
            line = _recommendation_line(item)
            if not line or line.lower() in seen:
                continue
            seen.add(line.lower())
            clean.append(line)
            if len(clean) >= CUT_RECOMMENDATIONS_CAP:
                break
        out["recommendations"] = clean
    if not out["recommendations"]:
        out["recommendations"] = _recommendations_from_actions(out)
    return out


async def call_gemini_cut(
    rows: list,
    label: str,
    given: str = "",
) -> dict:
    """One text-only pass over the group's write-ups."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    header = f"This group is: {label}\n"
    if given:
        header += "\n" + given + "\n"
    header += "\nThe write-ups for this group follow."
    parts = [
        types.Part.from_text(text=header),
        types.Part.from_text(text=_cut_side_text(rows, "SESSIONS")),
    ]
    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            system_instruction=CUT_PROMPT,
            # Concrete items carry timestamps, px depths and quoted labels, so
            # they run far longer than abstracted one-liners. Five sections of
            # them did not fit in the old 4096 — the model hit the ceiling and
            # summarised to fit, which is exactly the failure being fixed.
            # Thinking is budgeted separately from the answer, so both grew.
            max_output_tokens=12288,
            thinking_config=types.ThinkingConfig(thinking_budget=4096),
            temperature=0.2,
            response_mime_type="application/json",
        ),
    )
    return normalize_cut_analysis(_parse_json_object(response.text or ""))


async def build_cut_analysis(rows: list, slice_spec: Optional[dict]) -> dict:
    """Filter the cohort to one group and ask the model what it did."""
    if not ANALYSIS_ENABLED:
        raise RuntimeError("analysis is not configured — set GEMINI_API_KEY")

    selected, spec = select_rows_by_cut(rows, slice_spec)
    label = slice_label(spec) if spec else "all sessions"

    base = {
        "cut": cut_key(slice_spec),
        "label": label,
        "n": len(selected),
        "session_ids": [r.get("session_id") for r in selected],
        "model": GEMINI_MODEL,
        "created_at": int(time.time() * 1000),
    }

    # Nothing to read — say so rather than spending a call on an empty group.
    if not selected:
        empty = empty_cut_analysis()
        empty["headline"] = f"No analyzed sessions match {label} in this cohort."
        return {**base, "analysis": empty}

    given = _given_facts(spec)
    _, model_gate = _gates()
    async with model_gate:
        analysis = await call_gemini_cut(selected, label, given)
    return {**base, "analysis": analysis}


def sample_cohort_rows(
    conn: sqlite3.Connection,
    pool: str,
    purchased_n: int,
    all_n: int,
    *,
    min_duration_ms: int = COHORT_MIN_DURATION_MS,
    url_contains: str = COHORT_URL_CONTAINS,
) -> list:
    """Newest purchased + newest non-purchased sessions that match the filters.

    Prefers sessions with recorded clicks when breaking ties on recency, so thin
    bounce recordings are less likely to fill the sample. Duration is wall-clock
    (`last_seen - first_seen`). Empty `url_contains` disables the path filter.
    """
    activity = "coalesce(json_extract(interactions, '$.clicks'), 0)"
    where = [
        "pool = ?",
        "chunk_count > 0",
        "(last_seen - first_seen) >= ?",
    ]
    base_params: list = [pool, int(min_duration_ms)]
    needle = (url_contains or "").strip()
    if needle:
        where.append("url LIKE ?")
        base_params.append(f"%{needle}%")
    where_sql = " AND ".join(where)

    purchased = conn.execute(
        f"""
        SELECT session_id FROM sessions
        WHERE {where_sql}
          AND json_extract("order", '$.purchased_at') IS NOT NULL
        ORDER BY last_seen DESC, {activity} DESC, session_id DESC
        LIMIT ?
        """,
        (*base_params, purchased_n),
    ).fetchall()
    purchased_ids = {row[0] for row in purchased}

    all_rows = conn.execute(
        f"""
        SELECT session_id FROM sessions
        WHERE {where_sql}
          AND json_extract("order", '$.purchased_at') IS NULL
        ORDER BY last_seen DESC, {activity} DESC, session_id DESC
        LIMIT ?
        """,
        (*base_params, all_n + len(purchased_ids)),
    ).fetchall()

    sessions = [{"session_id": sid, "filter": "purchased", "status": "queued"} for (sid,) in purchased]
    for (sid,) in all_rows:
        if sid in purchased_ids:
            continue
        sessions.append({"session_id": sid, "filter": "all", "status": "queued"})
        if sum(1 for s in sessions if s["filter"] == "all") >= all_n:
            break
    return sessions


def cohort_selection_text(
    purchased_n: int,
    all_n: int,
    *,
    min_duration_ms: int = COHORT_MIN_DURATION_MS,
    url_contains: str = COHORT_URL_CONTAINS,
) -> str:
    """Human-readable sampling rule for the Insights UI."""
    page = (url_contains or "").strip() or "any page"
    min_s = max(0, int(min_duration_ms)) / 1000
    min_label = f"{min_s:g}s" if min_s != int(min_s) else f"{int(min_s)}s"
    return (
        f"Chosen as the latest {purchased_n} purchased + {all_n} non-purchase "
        f"sessions on {page} with duration ≥{min_label} (last_seen − first_seen), "
        f"preferring sessions with more clicks when timestamps tie."
    )


def build_cohort(
    conn: sqlite3.Connection,
    pool_key: str,
    purchased_n: int = COHORT_DEFAULT_PURCHASED_N,
    all_n: int = COHORT_DEFAULT_ALL_N,
    *,
    min_duration_ms: int = COHORT_MIN_DURATION_MS,
    url_contains: str = COHORT_URL_CONTAINS,
) -> dict:
    now = int(time.time() * 1000)
    sessions = sample_cohort_rows(
        conn,
        pool_key,
        purchased_n,
        all_n,
        min_duration_ms=min_duration_ms,
        url_contains=url_contains,
    )
    stamp = time.strftime("%b %-d, %H:%M", time.localtime(now / 1000))
    page = (url_contains or "").strip() or "all pages"
    min_s = max(0, int(min_duration_ms)) // 1000
    selection = cohort_selection_text(
        purchased_n,
        all_n,
        min_duration_ms=min_duration_ms,
        url_contains=url_contains,
    )
    return {
        "id": f"cohort-{now}",
        # Auto label; renameable once the user decides a cohort is worth keeping.
        "label": f"{purchased_n}+{all_n} · {page} · ≥{min_s}s · {stamp}",
        "selection": selection,
        "created_at": now,
        "updated_at": now,
        # Sampled only — analysis starts via POST .../analyze.
        "status": "idle",
        "purchased_n": purchased_n,
        "all_n": all_n,
        "min_duration_ms": int(min_duration_ms),
        "url_contains": (url_contains or "").strip(),
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


def session_chunk_names(session_dir: Path) -> list:
    if not session_dir.is_dir():
        return []
    return sorted(
        c.name for c in session_dir.iterdir() if c.is_file() and SAFE_CHUNK.match(c.name)
    )


def _usable_frames(frames: list) -> list:
    return [f for f in frames if Path(f.get("file") or "").is_file()]


def _session_model_parts(digest: dict, frames: list):
    from google.genai import types

    usable = _usable_frames(frames)
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
    return parts


def _session_model_config():
    from google.genai import types

    return types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        # max_output_tokens covers thinking as well as the answer, and a
        # long frame sequence makes the model think hard: at 1400 with no
        # budget, thinking ate all but ~50 tokens and the analysis came
        # back cut off mid-sentence.
        max_output_tokens=4000,
        thinking_config=types.ThinkingConfig(thinking_budget=1024),
        temperature=0.3,
    )


def build_inlined_session_request(session_id: str, digest: dict, frames: list):
    """One Batch API request for a session write-up, keyed by session_id."""
    from google.genai import types

    return types.InlinedRequest(
        contents=[types.Content(role="user", parts=_session_model_parts(digest, frames))],
        metadata={"session_id": session_id},
        config=_session_model_config(),
    )


def write_analysis_result(
    session_dir: Path,
    text: str,
    *,
    frame_count: int = 0,
    warnings: Optional[list] = None,
    model: Optional[str] = None,
) -> dict:
    analysis_path, _ = analysis_paths(session_dir)
    result = {
        "status": "done",
        "text": text,
        "model": model or GEMINI_MODEL,
        "created_at": int(time.time() * 1000),
        "frame_count": frame_count,
        "warnings": warnings or [],
    }
    analysis_path.write_text(json.dumps(result))
    clear_job(session_dir)
    return result


async def call_gemini(digest: dict, frames: list) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=[types.Content(role="user", parts=_session_model_parts(digest, frames))],
        config=_session_model_config(),
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty analysis")
    return text


def _estimate_inlined_bytes(requests: list) -> int:
    total = 0
    for req in requests:
        dumped = req.model_dump(mode="json", exclude_none=True)
        total += len(json.dumps(dumped))
    return total


async def submit_gemini_batch(requests: list, *, display_name: str) -> str:
    """Create a Batch job from already-built InlinedRequest objects.

    Prefer submit_prepared_gemini_batch for cohorts: building every request up
    front keeps all frame bytes in RAM and OOM'd a 2GB box at n=100.
    """
    from google import genai
    from google.genai import types

    if not requests:
        raise ValueError("batch requires at least one request")

    client = genai.Client(api_key=GEMINI_API_KEY)
    config = types.CreateBatchJobConfig(display_name=display_name[:128])
    size = _estimate_inlined_bytes(requests)
    if size <= ANALYZE_BATCH_INLINE_MAX_BYTES:
        job = await client.aio.batches.create(
            model=GEMINI_MODEL, src=requests, config=config
        )
    else:
        # JSONL + Files API — each line is {key, request} for result matching.
        # Write on disk (not tmpfs /tmp) so the payload does not compete for RAM.
        tmp = _batch_jsonl_path()
        try:
            with tmp.open("w") as out:
                for req in requests:
                    payload = req.model_dump(mode="json", exclude_none=True)
                    key = (payload.pop("metadata", None) or {}).get("session_id") or f"r{out.tell()}"
                    out.write(
                        json.dumps({"key": key, "request": payload}, separators=(",", ":"))
                    )
                    out.write("\n")
            uploaded = await client.aio.files.upload(
                file=str(tmp),
                config=types.UploadFileConfig(mime_type="jsonl"),
            )
        finally:
            tmp.unlink(missing_ok=True)
        job = await client.aio.batches.create(
            model=GEMINI_MODEL,
            src=types.BatchJobSource(file_name=uploaded.name),
            config=config,
        )
    if not job.name:
        raise RuntimeError("Gemini batch create returned no job name")
    return job.name


def _batch_jsonl_path(work_dir: Optional[Path] = None) -> Path:
    """Disk-backed temp path for batch JSONL (avoid tmpfs /tmp on small boxes)."""
    base = work_dir if work_dir is not None else Path("/var/tmp")
    base.mkdir(parents=True, exist_ok=True)
    return base / f"capture-batch-{int(time.time() * 1000)}.jsonl"


async def submit_prepared_gemini_batch(
    prepared: list,
    *,
    display_name: str,
    work_dir: Optional[Path] = None,
) -> str:
    """Create a Batch job from (entry, digest) pairs, one session at a time.

    Streams JSONL to disk so peak RAM is roughly one session's frames, not the
    whole cohort. Always uses the Files API — at cohort scale the inline path
    is both oversized and an OOM hazard while estimating size.
    """
    from google import genai
    from google.genai import types

    if not prepared:
        raise ValueError("batch requires at least one request")

    client = genai.Client(api_key=GEMINI_API_KEY)
    config = types.CreateBatchJobConfig(display_name=display_name[:128])
    tmp = _batch_jsonl_path(work_dir)
    try:
        with tmp.open("w") as out:
            for entry, digest in prepared:
                sid = entry["session_id"]
                req = build_inlined_session_request(
                    sid, digest, digest.get("frames") or []
                )
                payload = req.model_dump(mode="json", exclude_none=True)
                del req
                key = (payload.pop("metadata", None) or {}).get("session_id") or sid
                out.write(
                    json.dumps({"key": key, "request": payload}, separators=(",", ":"))
                )
                out.write("\n")
                del payload
        uploaded = await client.aio.files.upload(
            file=str(tmp),
            config=types.UploadFileConfig(mime_type="jsonl"),
        )
    finally:
        tmp.unlink(missing_ok=True)

    job = await client.aio.batches.create(
        model=GEMINI_MODEL,
        src=types.BatchJobSource(file_name=uploaded.name),
        config=config,
    )
    if not job.name:
        raise RuntimeError("Gemini batch create returned no job name")
    return job.name


def _batch_state_name(job) -> str:
    state = getattr(job, "state", None)
    if state is None:
        return ""
    return getattr(state, "name", None) or str(state)


def _batch_response_text(response) -> str:
    text = (getattr(response, "text", None) or "").strip()
    if text:
        return text
    # Some SDK shapes only expose candidates/parts.
    try:
        candidates = getattr(response, "candidates", None) or []
        parts = (candidates[0].content.parts if candidates else []) or []
        chunks = [getattr(p, "text", "") for p in parts if getattr(p, "text", None)]
        return "\n".join(chunks).strip()
    except Exception:
        return ""


def parse_batch_results(job) -> dict:
    """Map session_id → {"text": str} or {"error": str} from a finished batch job."""
    out: dict = {}
    dest = getattr(job, "dest", None)
    inlined = getattr(dest, "inlined_responses", None) if dest else None
    if inlined:
        for item in inlined:
            meta = getattr(item, "metadata", None) or {}
            sid = meta.get("session_id") if isinstance(meta, dict) else None
            if not sid:
                continue
            err = getattr(item, "error", None)
            if err:
                out[sid] = {
                    "error": getattr(err, "message", None) or str(err) or "batch request failed"
                }
                continue
            text = _batch_response_text(getattr(item, "response", None))
            if text:
                out[sid] = {"text": text}
            else:
                out[sid] = {"error": "Gemini returned an empty analysis"}
        return out

    # File-output jobs: download JSONL and match on `key`.
    file_name = getattr(dest, "file_name", None) if dest else None
    if not file_name:
        return out
    from google import genai

    client = genai.Client(api_key=GEMINI_API_KEY)
    raw = client.files.download(file=file_name)
    if isinstance(raw, bytes):
        content = raw.decode("utf-8", "replace")
    else:
        content = str(raw)
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        sid = row.get("key")
        if not sid:
            continue
        if row.get("error"):
            err = row["error"]
            out[sid] = {
                "error": err.get("message") if isinstance(err, dict) else str(err)
            }
            continue
        response = row.get("response") or {}
        text = ""
        if isinstance(response, dict):
            text = (response.get("text") or "").strip()
            if not text:
                try:
                    parts = response["candidates"][0]["content"]["parts"]
                    text = "\n".join(
                        p.get("text", "") for p in parts if isinstance(p, dict)
                    ).strip()
                except Exception:
                    text = ""
        if text:
            out[sid] = {"text": text}
        else:
            out[sid] = {"error": "Gemini returned an empty analysis"}
    return out


async def wait_gemini_batch(job_name: str, *, poll_s: Optional[float] = None):
    """Poll until the batch leaves a non-terminal state; return the final job."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    interval = ANALYZE_BATCH_POLL_S if poll_s is None else poll_s
    terminal = {
        types.JobState.JOB_STATE_SUCCEEDED,
        types.JobState.JOB_STATE_FAILED,
        types.JobState.JOB_STATE_CANCELLED,
        types.JobState.JOB_STATE_EXPIRED,
        types.JobState.JOB_STATE_PARTIALLY_SUCCEEDED,
    }
    while True:
        job = await client.aio.batches.get(name=job_name)
        state = getattr(job, "state", None)
        if state in terminal or _batch_state_name(job) in {
            s.name for s in terminal
        }:
            return job
        await asyncio.sleep(max(1.0, interval))


async def cancel_gemini_batch(job_name: str) -> None:
    """Best-effort cancel of an in-flight Batch job."""
    if not job_name:
        return
    from google import genai

    client = genai.Client(api_key=GEMINI_API_KEY)
    try:
        await client.aio.batches.cancel(name=job_name)
    except Exception as exc:
        print(f"batch cancel {job_name}: {exc}")


def mark_cohort_paused(pool: Path, cohort: dict) -> dict:
    """Persist paused status and return in-flight session rows to queued."""
    for entry in cohort.get("sessions") or []:
        session_dir = pool / entry["session_id"]
        if read_analysis(session_dir):
            entry["status"] = "done"
            entry["analysis"] = ANALYSIS_FILE
            entry.pop("error", None)
            continue
        if entry.get("status") in ("running", "queued") or (
            read_job(session_dir) or {}
        ).get("status") == "running":
            entry["status"] = "queued"
            entry.pop("error", None)
            clear_job(session_dir)
    cohort = {**cohort, "status": "paused"}
    cohort.pop("batch", None)
    write_cohort(pool, cohort)
    return enrich_cohort_status(pool, cohort)


async def extract_session_digest(
    session_dir: Path, chunk_names: list, *, force: bool = False
) -> dict:
    """Rebuild frames for a session. Returns the extractor digest."""
    if not force:
        cached = read_analysis(session_dir)
        if cached:
            return {"cached": True, "analysis": cached}

    _, out_dir = analysis_paths(session_dir)
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    extract_gate, _ = _gates()
    async with extract_gate:
        digest = await run_extractor(session_dir, out_dir, chunk_names)
    if digest.get("error"):
        raise RuntimeError(digest["error"])
    return digest


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

    digest = await extract_session_digest(session_dir, chunk_names, force=True)
    _, model_gate = _gates()
    async with model_gate:
        text = await call_gemini(digest, digest.get("frames") or [])
    return write_analysis_result(
        session_dir,
        text,
        frame_count=len(digest.get("frames") or []),
        warnings=digest.get("warnings") or [],
    )


def load_session_digest(session_dir: Path) -> Optional[dict]:
    """Read extractor digest.json if a prior reconstruction left one on disk."""
    path = session_dir / ANALYSIS_DIR / "digest.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def resume_cohort_batch(pool: Path, cohort: dict) -> dict:
    """Finish a cohort that already has a Gemini Batch job name persisted."""
    batch = cohort.get("batch") or {}
    job_name = batch.get("name")
    if not job_name:
        return enrich_cohort_status(pool, cohort)

    prepared = []
    wanted = set(batch.get("session_ids") or [])
    for entry in cohort.get("sessions") or []:
        sid = entry.get("session_id")
        if wanted and sid not in wanted:
            continue
        session_dir = pool / sid
        if read_analysis(session_dir):
            entry["status"] = "done"
            entry["analysis"] = ANALYSIS_FILE
            entry.pop("error", None)
            continue
        digest = load_session_digest(session_dir) or {"frames": [], "warnings": []}
        entry["status"] = "running"
        write_job(session_dir, "running")
        prepared.append((entry, digest))
    write_cohort(pool, cohort)

    if prepared:
        await apply_cohort_batch(pool, cohort, prepared, job_name=job_name)
    live = read_cohort(pool, cohort["id"]) or cohort
    live.pop("batch", None)
    final = enrich_cohort_status(pool, live)
    write_cohort(pool, final)
    return final


async def run_cohort_job(pool: Path, cohort: dict, *, force: bool = False) -> dict:
    """Extract cohort sessions, then analyze via Batch (or sync fallback).

    Mutates and persists the cohort as work progresses. Returns the enriched
    final cohort dict. Never deletes or overwrites other cohorts.
    """
    try:
        if (cohort.get("batch") or {}).get("name") and not force:
            return await resume_cohort_batch(pool, cohort)

        cohort = {**cohort, "status": "running"}
        cohort.pop("batch", None)
        write_cohort(pool, cohort)

        pending_entries = []
        for entry in cohort.get("sessions") or []:
            session_dir = pool / entry["session_id"]
            if not force and read_analysis(session_dir):
                entry["status"] = "done"
                entry["analysis"] = ANALYSIS_FILE
                entry.pop("error", None)
                continue
            entry["status"] = "queued"
            entry.pop("error", None)
            pending_entries.append(entry)
        write_cohort(pool, cohort)

        if not pending_entries:
            return enrich_cohort_status(pool, read_cohort(pool, cohort["id"]) or cohort)

        prepared = []  # (entry, digest)

        async def extract_one(entry: dict) -> None:
            session_dir = pool / entry["session_id"]
            entry["status"] = "running"
            write_job(session_dir, "running")
            write_cohort(pool, cohort)
            try:
                # Reuse a prior reconstruction unless the caller asked to redo.
                # Without this, a retry after a Batch OOM would wipe digests and
                # spend another hour in Playwright before talking to Gemini.
                if not force:
                    digest = load_session_digest(session_dir)
                    if digest and _usable_frames(digest.get("frames") or []):
                        prepared.append((entry, digest))
                        return
                chunks = session_chunk_names(session_dir)
                if not chunks:
                    raise RuntimeError("no chunks to analyze")
                if force:
                    clear_analysis(session_dir)
                digest = await extract_session_digest(session_dir, chunks, force=True)
                prepared.append((entry, digest))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                entry["status"] = "error"
                entry["error"] = str(exc) or "analysis failed"
                write_job(session_dir, "error", error=entry["error"])
                write_cohort(pool, cohort)

        await asyncio.gather(*(extract_one(e) for e in pending_entries), return_exceptions=True)

        if not prepared:
            return enrich_cohort_status(pool, read_cohort(pool, cohort["id"]) or cohort)

        use_batch = ANALYZE_BATCH and ANALYSIS_ENABLED
        if use_batch:
            try:
                job_name = await submit_prepared_gemini_batch(
                    prepared,
                    display_name=f"capture-{cohort['id']}",
                    work_dir=cohorts_dir(pool),
                )
                cohort["batch"] = {
                    "name": job_name,
                    "submitted_at": int(time.time() * 1000),
                    "session_ids": [e["session_id"] for e, _ in prepared],
                }
                write_cohort(pool, cohort)
                await apply_cohort_batch(pool, cohort, prepared, job_name=job_name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Fall back to interactive calls so a Batch outage does not strand
                # an already-extracted cohort.
                print(f"cohort batch failed ({exc}); falling back to sync Gemini")
                cohort.pop("batch", None)
                write_cohort(pool, cohort)
                await _cohort_sync_model(pool, cohort, prepared)
        else:
            await _cohort_sync_model(pool, cohort, prepared)

        live = read_cohort(pool, cohort["id"]) or cohort
        live.pop("batch", None)
        final = enrich_cohort_status(pool, live)
        write_cohort(pool, final)
        return final
    except asyncio.CancelledError:
        live = read_cohort(pool, cohort["id"]) or cohort
        batch_name = (live.get("batch") or {}).get("name")
        if batch_name:
            await cancel_gemini_batch(batch_name)
        mark_cohort_paused(pool, live)
        raise


async def apply_cohort_batch(
    pool: Path,
    cohort: dict,
    prepared: list,
    *,
    job_name: Optional[str] = None,
) -> None:
    """Wait for a batch job and write per-session analysis.json results."""
    name = job_name or (cohort.get("batch") or {}).get("name")
    if not name:
        raise RuntimeError("cohort has no batch job to apply")

    by_id = {entry["session_id"]: (entry, digest) for entry, digest in prepared}
    job = await wait_gemini_batch(name)
    state = _batch_state_name(job)
    if state in ("JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"):
        err = getattr(job, "error", None)
        message = getattr(err, "message", None) if err else None
        message = message or f"Gemini batch {state}"
        for entry, _ in prepared:
            entry["status"] = "error"
            entry["error"] = message
            write_job(pool / entry["session_id"], "error", error=message)
        write_cohort(pool, cohort)
        return

    results = parse_batch_results(job)
    for sid, (entry, digest) in by_id.items():
        session_dir = pool / sid
        hit = results.get(sid)
        if not hit:
            entry["status"] = "error"
            entry["error"] = "missing from batch results"
            write_job(session_dir, "error", error=entry["error"])
        elif hit.get("error"):
            entry["status"] = "error"
            entry["error"] = hit["error"]
            write_job(session_dir, "error", error=entry["error"])
        else:
            write_analysis_result(
                session_dir,
                hit["text"],
                frame_count=len(digest.get("frames") or []),
                warnings=digest.get("warnings") or [],
            )
            entry["status"] = "done"
            entry["analysis"] = ANALYSIS_FILE
            entry.pop("error", None)
        write_cohort(pool, cohort)


async def _cohort_sync_model(pool: Path, cohort: dict, prepared: list) -> None:
    """Interactive Gemini fallback used when Batch is off or unavailable."""

    async def one(entry: dict, digest: dict) -> None:
        session_dir = pool / entry["session_id"]
        entry["status"] = "running"
        write_job(session_dir, "running")
        write_cohort(pool, cohort)
        try:
            _, model_gate = _gates()
            async with model_gate:
                text = await call_gemini(digest, digest.get("frames") or [])
            write_analysis_result(
                session_dir,
                text,
                frame_count=len(digest.get("frames") or []),
                warnings=digest.get("warnings") or [],
            )
            entry["status"] = "done"
            entry["analysis"] = ANALYSIS_FILE
            entry.pop("error", None)
        except Exception as exc:
            entry["status"] = "error"
            entry["error"] = str(exc) or "analysis failed"
            write_job(session_dir, "error", error=entry["error"])
        write_cohort(pool, cohort)

    await asyncio.gather(
        *(one(entry, digest) for entry, digest in prepared),
        return_exceptions=True,
    )


def iter_session_pools(data_root: Path) -> list:
    """Session directories that may own a cohorts/ sibling."""
    pools = []
    legacy = data_root / "sessions"
    if legacy.is_dir():
        pools.append(legacy)
    projects = data_root / "projects"
    if projects.is_dir():
        for path in projects.iterdir():
            sessions = path / "sessions"
            if sessions.is_dir():
                pools.append(sessions)
    return pools


def cohorts_needing_batch_resume(data_root: Path) -> list:
    """(pool, cohort) pairs with an in-flight Batch job name on disk."""
    out = []
    for pool in iter_session_pools(data_root):
        for cohort in list_cohorts(pool):
            batch = cohort.get("batch") or {}
            if cohort.get("status") == "running" and batch.get("name"):
                out.append((pool, cohort))
    return out


def _cli_data_root() -> Path:
    return Path(os.environ.get("CAPTURE_DATA_DIR") or BASE_DIR / "data")


def _cli_pool(project: Optional[str]) -> Path:
    root = _cli_data_root()
    if not project:
        return root / "sessions"
    return root / "projects" / project / "sessions"


async def _cli_run_cohort(
    pool: Path,
    pool_key: str,
    purchased_n: int,
    all_n: int,
    force: bool,
    *,
    min_duration_ms: int,
    url_contains: str,
) -> int:
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
        cohort = build_cohort(
            conn,
            pool_key,
            purchased_n,
            all_n,
            min_duration_ms=min_duration_ms,
            url_contains=url_contains,
        )
    finally:
        conn.close()

    if not cohort["sessions"]:
        print("no sessions matched the purchased/all sample")
        write_cohort(pool, cohort)
        return 1

    write_cohort(pool, cohort)
    print(
        f"{cohort['label']}: {len(cohort['sessions'])} session(s) → "
        f"{cohort_path(pool, cohort['id'])}"
        f"{' [batch]' if ANALYZE_BATCH else ' [sync]'}"
    )
    final = await run_cohort_job(pool, cohort, force=force)
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
    cohort_p.add_argument(
        "--purchased",
        type=int,
        default=COHORT_DEFAULT_PURCHASED_N,
        help=f"purchased sample size (default {COHORT_DEFAULT_PURCHASED_N})",
    )
    cohort_p.add_argument(
        "--all",
        type=int,
        default=COHORT_DEFAULT_ALL_N,
        dest="all_n",
        help=f"all/non-purchase sample size (default {COHORT_DEFAULT_ALL_N})",
    )
    cohort_p.add_argument(
        "--min-duration-ms",
        type=int,
        default=COHORT_MIN_DURATION_MS,
        help=f"minimum last_seen-first_seen (default {COHORT_MIN_DURATION_MS})",
    )
    cohort_p.add_argument(
        "--url-contains",
        default=COHORT_URL_CONTAINS,
        help=f"require this path fragment in url (default {COHORT_URL_CONTAINS!r}; empty = any)",
    )
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
            _cli_run_cohort(
                pool,
                pool_key,
                args.purchased,
                args.all_n,
                args.force,
                min_duration_ms=args.min_duration_ms,
                url_contains=args.url_contains,
            )
        )

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

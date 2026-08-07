"""Session analysis: replay the session headlessly, then ask Gemini about it.

The extractor drives the same CapturePlayer the replay UI uses, so animations
are frozen and recorded scroll offsets are re-applied exactly as they are on
screen. Stepping that player yields a frame per second of activity — the page
as the visitor saw it change, rather than a single end-state snapshot.

Also owns the Phase-0 cohort sample: pick N purchased + N all sessions, persist
`cohort.json`, and leave the server to queue per-session analysis against it.
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
ANALYSIS_INSIGHTS_FILE = "analysis.insights.json"
ANALYSIS_QA_FILE = "analysis.qa.json"
ANALYSIS_STATUS_FILE = "analysis.status.json"
ANALYSIS_DIR = ".analysis"
COHORT_FILE = "cohort.json"
# Same shape as server.SAFE_CHUNK — keep local so the CLI does not import server.
SAFE_CHUNK = re.compile(r"^\d{1,16}(-[a-f0-9]{1,8})?-[a-z0-9]{6}\.json$")

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

# Controlled vocabularies — the model maps free text onto these tags (+ other:…).
# Grow slowly; free-form themes make cohort clustering noise.
THEME_VOCAB = (
    "size_chart_confusion",
    "shipping_hesitation",
    "price_hesitation",
    "trust_or_reviews",
    "navigation_confusion",
    "form_friction",
    "checkout_friction",
    "add_to_cart_friction",
    "product_info_gap",
    "promo_or_discount_seeking",
    "comparison_shopping",
    "rage_or_repeated_clicks",
    "slow_or_idle_on_page",
    "exit_before_checkout",
    "technical_or_asset_issues",
)
PAGE_VOCAB = ("home", "plp", "pdp", "cart", "checkout", "collection", "account", "other")
OUTCOME_SIGNAL_VOCAB = (
    "bounced_early",
    "browsed_products",
    "added_to_cart",
    "reached_checkout",
    "purchased",
    "abandoned_cart",
)
CONFIDENCE_VOCAB = ("low", "medium", "high")

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

INSIGHTS_PROMPT = """You turn one session analysis into structured tags for cohort comparison.

Use ONLY the measured digest and the markdown write-up. Do not invent pages,
clicks, or outcomes that are not evidenced there.

Return a single JSON object with this exact shape:
{
  "themes": ["…"],
  "friction": [{"atMs": 42000, "label": "short_snake_case"}],
  "pages": ["pdp", "cart"],
  "outcome_signals": ["reached_checkout"],
  "confidence": "low" | "medium" | "high"
}

themes — pick zero or more from this controlled vocabulary (prefer these):
""" + ", ".join(THEME_VOCAB) + """
If something important is missing, use other:<short_snake_case> (one theme).
Do not invent free-form theme strings outside the vocab or other:.

pages — only from: """ + ", ".join(PAGE_VOCAB) + """

outcome_signals — only from: """ + ", ".join(OUTCOME_SIGNAL_VOCAB) + """

friction.atMs is milliseconds from session start when known; omit entries you
cannot place. label is a short snake_case phrase grounded in the write-up.

confidence reflects how much evidence the digest and write-up give, not how
interesting the session felt. Prefer fewer accurate tags over many guesses."""


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
    (session_dir / ANALYSIS_INSIGHTS_FILE).unlink(missing_ok=True)
    (session_dir / ANALYSIS_QA_FILE).unlink(missing_ok=True)
    shutil.rmtree(session_dir / ANALYSIS_DIR, ignore_errors=True)


def insights_path(session_dir: Path) -> Path:
    return session_dir / ANALYSIS_INSIGHTS_FILE


def qa_path(session_dir: Path) -> Path:
    return session_dir / ANALYSIS_QA_FILE


def read_insights(session_dir: Path) -> Optional[dict]:
    path = insights_path(session_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_insights(session_dir: Path, insights: dict) -> dict:
    path = insights_path(session_dir)
    path.write_text(json.dumps(insights, indent=2))
    return insights


def read_qa(session_dir: Path) -> Optional[dict]:
    path = qa_path(session_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_qa(session_dir: Path, qa: dict) -> dict:
    path = qa_path(session_dir)
    path.write_text(json.dumps(qa, indent=2))
    return qa


def normalize_qa(raw: Optional[dict], insights: Optional[dict] = None) -> dict:
    """Normalize a QA payload. Themes default to insights themes when omitted."""
    base_themes = list((insights or {}).get("themes") or [])
    data = raw if isinstance(raw, dict) else {}
    status = data.get("status") or "pending"
    if status not in ("pending", "accepted", "rejected"):
        status = "pending"
    themes = []
    seen = set()
    source = data["themes"] if isinstance(data.get("themes"), list) else base_themes
    for item in source:
        theme = normalize_theme(item)
        if theme and theme not in seen:
            seen.add(theme)
            themes.append(theme)
    out = {
        "status": status,
        "themes": themes,
        "updated_at": int(data.get("updated_at") or time.time() * 1000),
    }
    note = data.get("note")
    if isinstance(note, str) and note.strip():
        out["note"] = note.strip()[:500]
    return out


def effective_themes(insights: Optional[dict], qa: Optional[dict]) -> list:
    """Themes used for rollups: QA override when present, else insights."""
    source = None
    if qa and isinstance(qa.get("themes"), list):
        source = qa["themes"]
    elif insights:
        source = insights.get("themes") or []
    else:
        source = []
    themes = []
    seen = set()
    for item in source:
        theme = normalize_theme(item)
        if theme and theme not in seen:
            seen.add(theme)
            themes.append(theme)
    return themes


def qa_allows_rollup(qa: Optional[dict], qa_mode: str = "include_pending") -> bool:
    """Whether this session's themes enter Compare rollups."""
    status = (qa or {}).get("status") or "pending"
    if qa_mode == "raw":
        return True
    if qa_mode == "accepted":
        return status == "accepted"
    # include_pending: accepted + pending/no-QA; exclude rejected
    return status != "rejected"


def empty_insights(error: Optional[str] = None) -> dict:
    out = {
        "themes": [],
        "friction": [],
        "pages": [],
        "outcome_signals": [],
        "confidence": "low",
    }
    if error:
        out["error"] = error
    return out


def _slug_tag(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower()).strip("_")
    return slug[:64]


def normalize_theme(raw) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    tag = raw.strip().lower()
    if not tag:
        return None
    if tag in THEME_VOCAB:
        return tag
    if tag.startswith("other:"):
        slug = _slug_tag(tag[6:])
        return f"other:{slug}" if slug else None
    # Free-form → other:<slug> so cohorts stay countable.
    slug = _slug_tag(tag)
    return f"other:{slug}" if slug else None


def normalize_insights(raw) -> dict:
    """Map model output onto the controlled vocabularies."""
    if not isinstance(raw, dict):
        return empty_insights("insights were not an object")

    themes = []
    seen = set()
    for item in raw.get("themes") or []:
        theme = normalize_theme(item)
        if theme and theme not in seen:
            seen.add(theme)
            themes.append(theme)

    pages = []
    seen_pages = set()
    for item in raw.get("pages") or []:
        if not isinstance(item, str):
            continue
        page = item.strip().lower()
        if page in PAGE_VOCAB and page not in seen_pages:
            seen_pages.add(page)
            pages.append(page)

    signals = []
    seen_signals = set()
    for item in raw.get("outcome_signals") or []:
        if not isinstance(item, str):
            continue
        signal = item.strip().lower()
        if signal in OUTCOME_SIGNAL_VOCAB and signal not in seen_signals:
            seen_signals.add(signal)
            signals.append(signal)

    friction = []
    for item in raw.get("friction") or []:
        if not isinstance(item, dict):
            continue
        label = _slug_tag(str(item.get("label") or ""))
        if not label:
            continue
        entry = {"label": label}
        at = item.get("atMs")
        if isinstance(at, (int, float)) and not isinstance(at, bool) and at >= 0:
            entry["atMs"] = int(at)
        friction.append(entry)

    confidence = raw.get("confidence")
    if confidence not in CONFIDENCE_VOCAB:
        confidence = "low" if not themes else "medium"

    return {
        "themes": themes,
        "friction": friction,
        "pages": pages,
        "outcome_signals": signals,
        "confidence": confidence,
    }


def cohort_path(pool: Path) -> Path:
    """cohort.json lives next to the sessions directory (project root / data root)."""
    return pool.parent / COHORT_FILE


def read_cohort(pool: Path) -> Optional[dict]:
    path = cohort_path(pool)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_cohort(pool: Path, cohort: dict) -> dict:
    cohort = {**cohort, "updated_at": int(time.time() * 1000)}
    path = cohort_path(pool)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cohort, indent=2))
    return cohort


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
        row.pop("themes", None)
        row.pop("insights", None)
        row.pop("insights_file", None)
        row.pop("attribution", None)
        row.pop("qa", None)
        row.pop("qa_status", None)
        session_dir = pool / row["session_id"]
        cached = read_analysis(session_dir)
        job = read_job(session_dir)
        if cached:
            row["status"] = "done"
            row["analysis"] = ANALYSIS_FILE
            if insights_path(session_dir).is_file():
                row["insights_file"] = ANALYSIS_INSIGHTS_FILE
            qa = read_qa(session_dir)
            if qa:
                row["qa_status"] = qa.get("status") or "pending"
            else:
                row["qa_status"] = "pending"
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
    """Attach analysis write-ups for Compare — response-only, not written to cohort.json."""
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
            insights = read_insights(session_dir)
            qa = read_qa(session_dir)
            if qa:
                row["qa"] = qa
                row["qa_status"] = qa.get("status") or "pending"
            else:
                row["qa_status"] = "pending"
            if insights or qa:
                themes = effective_themes(insights, qa)
                row["themes"] = themes
                if insights:
                    row["insights"] = {**insights, "themes": themes}
        sessions.append(row)
    return {**cohort, "sessions": sessions}


def count_themes(theme_lists) -> dict:
    """theme → count, sorted by frequency then name."""
    counts: dict = {}
    for themes in theme_lists:
        for theme in themes or []:
            if not isinstance(theme, str) or not theme:
                continue
            counts[theme] = counts.get(theme, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def theme_examples(rows: list, theme: str, limit: int = 5) -> list:
    """Short example cards for sessions that carry a theme."""
    examples = []
    for row in rows:
        themes = row.get("themes") or []
        if theme not in themes:
            continue
        attr = row.get("attribution") if isinstance(row.get("attribution"), dict) else {}
        examples.append(
            {
                "session_id": row.get("session_id"),
                "outcome": row.get("outcome"),
                "standout": row.get("standout") or "",
                "campaign": attr.get("utm_campaign"),
                "utm_source": attr.get("utm_source"),
                "meta": bool(attr.get("fbclid")) or is_meta_attribution(attr),
                "themes": themes,
            }
        )
        if len(examples) >= limit:
            break
    return examples


def annotate_theme_stats(theme_counts_map: dict, rows: list) -> list:
    """List of {theme, count, one_off, examples} sorted by frequency."""
    out = []
    for theme, count in sorted(theme_counts_map.items(), key=lambda kv: (-kv[1], kv[0])):
        out.append(
            {
                "theme": theme,
                "count": count,
                "one_off": count == 1,
                "examples": theme_examples(rows, theme),
            }
        )
    return out


def theme_cooccurrence(rows: list, limit: int = 10) -> list:
    """Top theme pairs that co-occur in the same session."""
    pair_counts: dict = {}
    for row in rows:
        themes = sorted({t for t in (row.get("themes") or []) if isinstance(t, str) and t})
        for i, a in enumerate(themes):
            for b in themes[i + 1 :]:
                key = (a, b)
                pair_counts[key] = pair_counts.get(key, 0) + 1
    ranked = sorted(pair_counts.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))
    return [
        {"themes": [a, b], "count": n}
        for (a, b), n in ranked[:limit]
        if n >= 2
    ]


def segment_label(attribution: Optional[dict], group_by: str) -> str:
    if not group_by or group_by == "none":
        return "(all)"
    attrs = attribution if isinstance(attribution, dict) else {}
    if group_by == "fbclid":
        return "meta" if attrs.get("fbclid") else "(none)"
    value = attrs.get(group_by)
    return str(value) if value else "(none)"


META_SOURCES = frozenset({"facebook", "meta", "fb", "instagram", "ig"})


def is_meta_attribution(attribution: Optional[dict]) -> bool:
    attrs = attribution if isinstance(attribution, dict) else {}
    if attrs.get("fbclid"):
        return True
    source = str(attrs.get("utm_source") or "").strip().lower()
    return source in META_SOURCES


def normalize_slice(spec: Optional[dict]) -> dict:
    """Normalize a slice dict: outcome + optional attribution constraints.

    Keys: outcome ("purchased"|"all"|None), utm_source, utm_medium, utm_campaign,
    utm_content, fbclid (True|False|None). utm_source="meta" matches Meta traffic
    (fbclid or known Meta source values).
    """
    if not isinstance(spec, dict):
        return {}
    out = {}
    outcome = spec.get("outcome")
    if outcome in ("purchased", "all"):
        out["outcome"] = outcome
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
    outcome = row.get("outcome")
    if spec.get("outcome") == "purchased" and outcome != "purchased":
        return False
    if spec.get("outcome") == "all" and outcome == "purchased":
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


def slice_label(spec: dict) -> str:
    if not spec:
        return "everyone else"
    parts = []
    if spec.get("outcome") == "purchased":
        parts.append("purchased")
    elif spec.get("outcome") == "all":
        parts.append("non-purchase")
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
    if len(parts) == 1:
        return parts[0]
    # "Meta + purchased" reads better than "purchased + Meta"
    if "Meta" in parts and "purchased" in parts:
        rest = [p for p in parts if p not in ("Meta", "purchased")]
        return "Meta + purchased" + ("" if not rest else " · " + " · ".join(rest))
    return " + ".join(parts)


def _theme_rates(rows: list) -> tuple:
    """Return (n_sessions, theme→session_count)."""
    n = len(rows)
    counts: dict = {}
    for row in rows:
        seen = set()
        for theme in row.get("themes") or []:
            if isinstance(theme, str) and theme and theme not in seen:
                seen.add(theme)
                counts[theme] = counts.get(theme, 0) + 1
    return n, counts


def _fmt_ratio(ratio: float) -> str:
    if ratio >= 10:
        return f"{ratio:.0f}×"
    text = f"{ratio:.1f}"
    if text.endswith(".0"):
        text = text[:-2]
    return f"{text}×"


def theme_display_name(theme: str) -> str:
    raw = theme[6:] if theme.startswith("other:") else theme
    return raw.replace("_", " ")


def contrast_narrative(contrast: dict) -> list:
    """Deterministic sentences from A/B theme rates — no model call."""
    a = contrast.get("a") or {}
    b = contrast.get("b") or {}
    a_label = a.get("label") or "slice A"
    b_label = b.get("label") or "slice B"
    n_a, n_b = a.get("n") or 0, b.get("n") or 0
    lines = []
    if n_a == 0 and n_b == 0:
        return ["No analyzed sessions match this contrast yet."]
    if n_a == 0:
        return [f"No sessions in {a_label} to compare against {b_label}."]
    if n_b == 0:
        return [f"Every analyzed session is in {a_label} — nothing left in {b_label}."]

    for item in (contrast.get("enriched_a") or [])[:3]:
        theme = theme_display_name(item["theme"])
        a_c, b_c = item["a_count"], item["b_count"]
        ratio = item.get("ratio")
        if ratio and ratio >= 1.5 and b_c > 0:
            lines.append(
                f"{theme} shows up {_fmt_ratio(ratio)} more often in {a_label} "
                f"({a_c}/{n_a}) than in {b_label} ({b_c}/{n_b})."
            )
        elif b_c == 0 and a_c > 0:
            lines.append(
                f"{theme} appears in {a_c}/{n_a} {a_label} sessions and nowhere in {b_label}."
            )
        else:
            lines.append(
                f"{theme} is more common in {a_label} ({a_c}/{n_a}) than {b_label} ({b_c}/{n_b})."
            )

    for item in (contrast.get("enriched_b") or [])[:2]:
        theme = theme_display_name(item["theme"])
        a_c, b_c = item["a_count"], item["b_count"]
        ratio = item.get("ratio")
        if ratio and ratio >= 1.5 and a_c > 0:
            lines.append(
                f"{theme} shows up {_fmt_ratio(ratio)} more often in {b_label} "
                f"({b_c}/{n_b}) than in {a_label} ({a_c}/{n_a})."
            )
        elif a_c == 0 and b_c > 0:
            lines.append(
                f"{theme} appears in {b_c}/{n_b} {b_label} sessions and nowhere in {a_label}."
            )

    shared = contrast.get("shared") or []
    if shared and not lines:
        names = ", ".join(theme_display_name(s["theme"]) for s in shared[:3])
        lines.append(f"Both slices share {names}.")
    elif shared and len(lines) < 2:
        top = shared[0]
        lines.append(
            f"Both sides see {theme_display_name(top['theme'])} "
            f"({top['a_count']}/{n_a} vs {top['b_count']}/{n_b})."
        )

    if not lines:
        lines.append(
            f"No strong theme differences between {a_label} (n={n_a}) and {b_label} (n={n_b})."
        )
    return lines


def build_slice_contrast(rows: list, slice_a: Optional[dict], slice_b: Optional[dict] = None) -> dict:
    """A/B theme contrast. If slice_b is empty, B is the complement of A."""
    spec_a = normalize_slice(slice_a)
    spec_b = normalize_slice(slice_b)
    if slice_is_empty(spec_a) and slice_is_empty(spec_b):
        # Default: purchased vs non-purchase
        spec_a = {"outcome": "purchased"}
        spec_b = {"outcome": "all"}
        complement = False
    elif slice_is_empty(spec_b):
        complement = True
    else:
        complement = False

    rows_a, rows_b = [], []
    for row in rows:
        in_a = row_matches_slice(row, spec_a)
        if complement:
            in_b = not in_a
        else:
            in_b = row_matches_slice(row, spec_b)
        # A session matching both custom slices counts in A only (exclusive A).
        if in_a and in_b and not complement:
            rows_a.append(row)
        elif in_a:
            rows_a.append(row)
        elif in_b:
            rows_b.append(row)

    label_a = slice_label(spec_a)
    label_b = "everyone else" if complement else slice_label(spec_b)
    n_a, themes_a = _theme_rates(rows_a)
    n_b, themes_b = _theme_rates(rows_b)
    all_themes = sorted(set(themes_a) | set(themes_b))

    def rate(count, n):
        return (count / n) if n else 0.0

    enriched_a, enriched_b, shared = [], [], []
    for theme in all_themes:
        a_c = themes_a.get(theme, 0)
        b_c = themes_b.get(theme, 0)
        a_r, b_r = rate(a_c, n_a), rate(b_c, n_b)
        item = {
            "theme": theme,
            "a_count": a_c,
            "b_count": b_c,
            "a_rate": round(a_r, 4),
            "b_rate": round(b_r, 4),
            "ratio": round(a_r / b_r, 2) if b_r > 0 else (None if a_c == 0 else None),
        }
        # Prefer rate delta for "enriched".
        if a_r > b_r + 1e-12:
            if b_r > 0:
                item["ratio"] = round(a_r / b_r, 2)
            enriched_a.append(item)
        elif b_r > a_r + 1e-12:
            inv = dict(item)
            if a_r > 0:
                inv["ratio"] = round(b_r / a_r, 2)
            else:
                inv["ratio"] = None
            enriched_b.append(inv)
        elif a_c > 0 and b_c > 0:
            shared.append(item)

    enriched_a.sort(key=lambda x: (-(x["a_rate"] - x["b_rate"]), -x["a_count"], x["theme"]))
    enriched_b.sort(key=lambda x: (-(x["b_rate"] - x["a_rate"]), -x["b_count"], x["theme"]))
    shared.sort(key=lambda x: (-min(x["a_count"], x["b_count"]), x["theme"]))

    for item in enriched_a:
        item["one_off"] = item["a_count"] == 1
        item["examples"] = theme_examples(rows_a, item["theme"])
    for item in enriched_b:
        item["one_off"] = item["b_count"] == 1
        item["examples"] = theme_examples(rows_b, item["theme"])
    for item in shared:
        item["examples"] = theme_examples(rows_a + rows_b, item["theme"])

    contrast = {
        "a": {
            "label": label_a,
            "spec": spec_a,
            "n": n_a,
            "themes": dict(sorted(themes_a.items(), key=lambda kv: (-kv[1], kv[0]))),
            "theme_details": annotate_theme_stats(themes_a, rows_a),
            "sessions": [
                {
                    "session_id": r.get("session_id"),
                    "outcome": r.get("outcome"),
                    "themes": r.get("themes") or [],
                    "standout": r.get("standout") or "",
                    "attribution": r.get("attribution"),
                }
                for r in rows_a
            ],
        },
        "b": {
            "label": label_b,
            "spec": {"complement": True} if complement else spec_b,
            "n": n_b,
            "themes": dict(sorted(themes_b.items(), key=lambda kv: (-kv[1], kv[0]))),
            "theme_details": annotate_theme_stats(themes_b, rows_b),
            "sessions": [
                {
                    "session_id": r.get("session_id"),
                    "outcome": r.get("outcome"),
                    "themes": r.get("themes") or [],
                    "standout": r.get("standout") or "",
                    "attribution": r.get("attribution"),
                }
                for r in rows_b
            ],
        },
        "complement": complement,
        "enriched_a": enriched_a,
        "enriched_b": enriched_b,
        "shared": shared,
    }
    contrast["narrative"] = contrast_narrative(contrast)
    return contrast


def build_insights_summary(
    rows: list,
    group_by: str = "none",
    slice_a: Optional[dict] = None,
    slice_b: Optional[dict] = None,
) -> dict:
    """Roll theme counts by outcome and optional attribution segment.

    Each row: {session_id, outcome ("purchased"|"all"), attribution, themes}.
    Always includes an A/B contrast (default purchased vs non-purchase).
    """
    group_by = group_by if group_by in (
        "utm_source", "utm_medium", "utm_campaign", "utm_content", "fbclid", "none"
    ) else "none"

    by_outcome = {"purchased": [], "all": []}
    by_segment: dict = {}
    for row in rows:
        outcome = "purchased" if row.get("outcome") == "purchased" else "all"
        themes = list(row.get("themes") or [])
        by_outcome[outcome].append(themes)
        label = segment_label(row.get("attribution"), group_by)
        bucket = by_segment.setdefault(label, {"n": 0, "theme_lists": [], "sessions": []})
        bucket["n"] += 1
        bucket["theme_lists"].append(themes)
        bucket["sessions"].append(
            {
                "session_id": row.get("session_id"),
                "outcome": outcome,
                "themes": themes,
            }
        )

    outcome_summary = {}
    for key, lists in by_outcome.items():
        outcome_summary[key] = {
            "n": len(lists),
            "themes": count_themes(lists),
        }

    purchased_themes = outcome_summary["purchased"]["themes"]
    all_themes = outcome_summary["all"]["themes"]
    all_keys = sorted(set(purchased_themes) | set(all_themes))
    enriched = {
        "purchased": [
            {"theme": t, "purchased": purchased_themes.get(t, 0), "all": all_themes.get(t, 0)}
            for t in all_keys
            if purchased_themes.get(t, 0) > all_themes.get(t, 0)
        ],
        "all": [
            {"theme": t, "purchased": purchased_themes.get(t, 0), "all": all_themes.get(t, 0)}
            for t in all_keys
            if all_themes.get(t, 0) > purchased_themes.get(t, 0)
        ],
    }

    segments = []
    for label, bucket in sorted(by_segment.items(), key=lambda kv: (-kv[1]["n"], kv[0])):
        themes = count_themes(bucket["theme_lists"])
        segments.append(
            {
                "segment": label,
                "n": bucket["n"],
                "themes": themes,
                "sessions": bucket["sessions"],
            }
        )

    theme_names = sorted(
        {t for seg in segments for t in seg["themes"]},
        key=lambda t: (-sum(seg["themes"].get(t, 0) for seg in segments), t),
    )
    col_labels = [seg["segment"] for seg in segments]
    matrix = [[seg["themes"].get(theme, 0) for seg in segments] for theme in theme_names]

    contrast = build_slice_contrast(rows, slice_a, slice_b)
    all_theme_counts = count_themes(by_outcome["purchased"] + by_outcome["all"])

    return {
        "group_by": group_by,
        "n": len(rows),
        "themes": [
            {"theme": theme, "count": count, "one_off": count == 1}
            for theme, count in all_theme_counts.items()
        ],
        "theme_details": annotate_theme_stats(all_theme_counts, rows),
        "cooccurrence": theme_cooccurrence(rows),
        "by_outcome": outcome_summary,
        "enriched": enriched,
        "by_segment": segments,
        "theme_x_segment": {
            "rows": theme_names,
            "cols": col_labels,
            "counts": matrix,
        },
        "contrast": contrast,
        "narrative": contrast.get("narrative") or [],
    }


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
    return {
        "id": f"cohort-{now}",
        "label": f"Sample {purchased_n}+{all_n}",
        "created_at": now,
        "updated_at": now,
        "status": "queued",
        "purchased_n": purchased_n,
        "all_n": all_n,
        "sessions": sessions,
    }


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
        raise ValueError("empty insights response")
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        raw = fence.group(1).strip()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("insights response was not an object")
    return data


async def call_gemini_insights(digest: dict, markdown: str) -> dict:
    """Cheap text-only pass: map digest + markdown onto the controlled vocab."""
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)
    parts = [
        types.Part.from_text(
            text="Measured digest for this session:\n\n"
            + json.dumps(prompt_digest(digest), indent=2)
        ),
        types.Part.from_text(text="\nMarkdown write-up for this session:\n\n" + markdown),
    ]
    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            system_instruction=INSIGHTS_PROMPT,
            max_output_tokens=1024,
            thinking_config=types.ThinkingConfig(thinking_budget=256),
            temperature=0.1,
            response_mime_type="application/json",
        ),
    )
    return normalize_insights(_parse_json_object(response.text or ""))


async def analyze_session(session_dir: Path, chunk_names: list, force: bool = False) -> dict:
    if not ANALYSIS_ENABLED:
        raise RuntimeError("analysis is not configured — set GEMINI_API_KEY")

    if not force:
        cached = read_analysis(session_dir)
        if cached:
            insights = read_insights(session_dir)
            if insights:
                return {**cached, "insights": insights}
            return cached

    analysis_path, out_dir = analysis_paths(session_dir)
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    insights_path(session_dir).unlink(missing_ok=True)

    extract_gate, model_gate = _gates()

    async with extract_gate:
        digest = await run_extractor(session_dir, out_dir, chunk_names)
    if digest.get("error"):
        raise RuntimeError(digest["error"])

    # Held across both calls: the second one only makes sense for the write-up
    # the first produced, and re-queueing between them would just add latency.
    async with model_gate:
        text = await call_gemini(digest, digest.get("frames") or [])

        try:
            insights = await call_gemini_insights(digest, text)
        except Exception as exc:
            insights = empty_insights(str(exc))
    insights = {
        **insights,
        "model": GEMINI_MODEL,
        "created_at": int(time.time() * 1000),
    }
    write_insights(session_dir, insights)

    result = {
        "status": "done",
        "text": text,
        "model": GEMINI_MODEL,
        "created_at": int(time.time() * 1000),
        "frame_count": len(digest.get("frames") or []),
        "warnings": digest.get("warnings") or [],
        "insights": insights,
    }
    analysis_path.write_text(json.dumps({k: v for k, v in result.items() if k != "insights"}))
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
    print(f"{cohort['label']}: {len(cohort['sessions'])} session(s) → {cohort_path(pool)}")

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

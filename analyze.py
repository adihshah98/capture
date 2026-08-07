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

EXTRACTOR = BASE_DIR / "src" / "analyze" / "extract.mjs"
REPLAYER_BUNDLE = BASE_DIR / "src" / "analyze" / "replayer.bundle.js"

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
    shutil.rmtree(session_dir / ANALYSIS_DIR, ignore_errors=True)


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
    """Attach analysis write-ups for Compare — response-only, not written to cohort.json."""
    sessions = []
    for entry in cohort.get("sessions") or []:
        row = dict(entry)
        if row.get("status") == "done":
            cached = read_analysis(pool / row["session_id"])
            if cached and isinstance(cached.get("text"), str) and cached["text"].strip():
                row["text"] = cached["text"]
                row["standout"] = standout_line(cached["text"])
                if cached.get("model"):
                    row["model"] = cached["model"]
                if cached.get("frame_count") is not None:
                    row["frame_count"] = cached["frame_count"]
        sessions.append(row)
    return {**cohort, "sessions": sessions}


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

    digest = await run_extractor(session_dir, out_dir, chunk_names)
    if digest.get("error"):
        raise RuntimeError(digest["error"])

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

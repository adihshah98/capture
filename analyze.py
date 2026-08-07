"""Session analysis: replay the session headlessly, then ask Gemini about it.

The extractor drives the same CapturePlayer the replay UI uses, so animations
are frozen and recorded scroll offsets are re-applied exactly as they are on
screen. Stepping that player yields a frame per second of activity — the page
as the visitor saw it change, rather than a single end-state snapshot.
"""

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent

ANALYSIS_FILE = "analysis.json"
ANALYSIS_DIR = ".analysis"

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


def read_analysis(session_dir: Path) -> Optional[dict]:
    path = session_dir / ANALYSIS_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


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
        "text": text,
        "model": GEMINI_MODEL,
        "created_at": int(time.time() * 1000),
        "frame_count": len(digest.get("frames") or []),
        "warnings": digest.get("warnings") or [],
    }
    analysis_path.write_text(json.dumps(result))
    return result

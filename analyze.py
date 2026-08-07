"""Session analysis: reconstruct the page, measure attention, ask Gemini about it.

The extractor rebuilds the recorded DOM with the same rrweb Replayer the replay
UI uses, so images load from the customer's origin exactly as they do on screen.
That single pass yields both element geometry (rrweb snapshots store no
coordinates) and screenshots of the regions the visitor actually dwelled on.
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
ANALYZE_TIMEOUT_S = float(os.environ.get("ANALYZE_TIMEOUT_S", "180"))
MAX_SHOTS_TO_MODEL = int(os.environ.get("ANALYZE_MAX_SHOTS", "6"))

ANALYSIS_ENABLED = bool(GEMINI_API_KEY)

EXTRACTOR = BASE_DIR / "src" / "analyze" / "extract.mjs"
REPLAYER_BUNDLE = BASE_DIR / "src" / "analyze" / "replayer.bundle.js"

SYSTEM_PROMPT = """You analyse one recorded web session for a product team.

You receive a measured digest and screenshots of the regions the visitor kept on
screen longest. The dwell numbers are measured from scroll events, with idle
gaps already removed. The screenshots show what the visitor actually saw.

Ground every claim in the digest. Never invent a section, image, or number that
is not there. If the warnings mention images that failed to load, do not
describe what those regions look like.

Say "on screen", not "looked at" or "read". Scroll position proves a region was
displayed; it does not prove attention. This distinction matters — do not blur
it.

Write four short markdown sections:

## Where attention went
Which sections held the visitor longest, with the measured seconds. Name what is
in them, using the screenshots for anything visual (product imagery, supplement
facts, trust signals like a doctor's endorsement or review counts).

## What they skipped
Sections passed quickly or never reached.

## Engagement and friction
What they clicked, in order. Call out rage clicks and repeated interactions.

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


def prompt_digest(digest: dict) -> dict:
    """Trim the digest to what the model needs, in seconds rather than ms."""
    session = digest.get("session") or {}
    return {
        "session": {
            "duration_s": _seconds(session.get("durationMs")),
            "active_s": _seconds(session.get("activeMs")),
            "idle_s": _seconds(session.get("idleMs")),
            "viewport": session.get("viewport"),
            "page_height_px": session.get("pageHeight"),
            "pages": session.get("urls") or [],
        },
        "sections_by_time_on_screen": [
            {
                "label": s.get("label"),
                "seconds_on_screen": _seconds(s.get("dwellMs")),
                "trust_signal": s.get("trustSignal", False),
                "images": [
                    {"alt": i.get("alt") or None, "loaded": i.get("loaded", True)}
                    for i in (s.get("images") or [])
                ],
            }
            for s in (digest.get("sections") or [])
        ],
        "clicks_in_order": [
            {
                "at_s": _seconds(c.get("atMs")),
                "label": c.get("text") or None,
                "element": c.get("tag") or None,
                "in_section": c.get("section"),
                "image_alt": (c.get("image") or {}).get("alt"),
                "rage_click": c.get("rage", False),
            }
            for c in (digest.get("clicks") or [])
        ],
        "warnings": digest.get("warnings") or [],
    }


async def call_gemini(digest: dict, shots: list) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=GEMINI_API_KEY)

    parts = [
        types.Part.from_text(
            text="Measured digest for this session:\n\n"
            + json.dumps(prompt_digest(digest), indent=2)
        )
    ]
    for shot in shots[:MAX_SHOTS_TO_MODEL]:
        path = Path(shot["file"])
        if not path.is_file():
            continue
        parts.append(
            types.Part.from_text(text=f"Screenshot of the section titled: {shot['label']}")
        )
        parts.append(
            types.Part.from_bytes(data=path.read_bytes(), mime_type="image/jpeg")
        )

    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            max_output_tokens=1400,
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

    text = await call_gemini(digest, digest.get("screenshots") or [])

    result = {
        "text": text,
        "model": GEMINI_MODEL,
        "created_at": int(time.time() * 1000),
        "sections": [
            {"label": s.get("label"), "seconds": _seconds(s.get("dwellMs"))}
            for s in (digest.get("sections") or [])[:8]
        ],
        "warnings": digest.get("warnings") or [],
    }
    analysis_path.write_text(json.dumps(result))
    return result

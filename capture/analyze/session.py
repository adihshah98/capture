"""Extractor + single-session analyze path.

Owns headless reconstruction (`run_extractor`), digest load/extract helpers, and
`analyze_session` (interactive Gemini write-up). Cohort orchestration imports
this module and calls through it so tests can
`mock.patch("capture.analyze.session.run_extractor", ...)` at the definition site.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

from ..paths import SAFE_CHUNK
from . import gemini, runtime
from .storage import (
    ANALYSIS_DIR,
    analysis_paths,
    clear_job,
    read_analysis,
    write_analysis,
)


async def run_extractor(session_dir: Path, out_dir: Path, chunk_names: list) -> dict:
    if not shutil.which("node"):
        raise RuntimeError("node is not installed on this server — analysis is unavailable")
    if not runtime.REPLAYER_BUNDLE.is_file():
        raise RuntimeError("replayer bundle missing — run `npm run build`")

    proc = await asyncio.create_subprocess_exec(
        "node",
        str(runtime.EXTRACTOR),
        str(session_dir),
        str(out_dir),
        str(runtime.REPLAYER_BUNDLE),
        # Named flags — never positional ahead of the variadic chunk list.
        # A misplaced arg used to make Number(chunkName) → NaN → maxFrames=0
        # and drop that chunk from the analysis with no error.
        "--fps",
        str(runtime.ANALYZE_FPS),
        "--max-frames",
        str(runtime.ANALYZE_MAX_FRAMES),
        *chunk_names,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(runtime.BASE_DIR),
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), runtime.ANALYZE_TIMEOUT_S)
    except TimeoutError as exc:
        proc.kill()
        raise RuntimeError("page reconstruction timed out") from exc

    if proc.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(
            f"page reconstruction failed: {detail[-1] if detail else 'unknown error'}"
        )

    try:
        return json.loads(stdout.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("page reconstruction returned no digest") from exc


def session_chunk_names(session_dir: Path) -> list:
    if not session_dir.is_dir():
        return []
    return sorted(c.name for c in session_dir.iterdir() if c.is_file() and SAFE_CHUNK.match(c.name))


def write_analysis_result(
    project_id: str,
    session_id: str,
    text: str,
    *,
    frame_count: int = 0,
    warnings: list | None = None,
    model: str | None = None,
) -> dict:
    result = {
        "status": "done",
        "text": text,
        "model": model or runtime.GEMINI_MODEL,
        "created_at": int(time.time() * 1000),
        "frame_count": frame_count,
        "warnings": warnings or [],
    }
    write_analysis(project_id, session_id, result)
    clear_job(project_id, session_id)
    return result


async def extract_session_digest(
    session_dir: Path, chunk_names: list, *, project_id: str, force: bool = False
) -> dict:
    """Rebuild frames for a session. Returns the extractor digest."""
    if not force:
        cached = read_analysis(project_id, session_dir.name)
        if cached:
            return {"cached": True, "analysis": cached}

    _, out_dir = analysis_paths(session_dir)
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    extract_gate, _ = runtime._gates()
    async with extract_gate:
        digest = await run_extractor(session_dir, out_dir, chunk_names)
    if digest.get("error"):
        raise RuntimeError(digest["error"])
    return digest


async def analyze_session(
    project_id: str, session_dir: Path, chunk_names: list, force: bool = False
) -> dict:
    if not runtime.ANALYSIS_ENABLED:
        raise RuntimeError("analysis is not configured — set GEMINI_API_KEY")

    session_id = session_dir.name
    if not force:
        cached = read_analysis(project_id, session_id)
        if cached:
            return cached

    digest = await extract_session_digest(
        session_dir, chunk_names, project_id=project_id, force=True
    )
    meta = gemini.session_metadata(project_id, session_id)
    _, model_gate = runtime._gates()
    async with model_gate:
        text = await gemini.call_gemini(digest, digest.get("frames") or [], meta)
    return write_analysis_result(
        project_id,
        session_id,
        text,
        frame_count=len(digest.get("frames") or []),
        warnings=digest.get("warnings") or [],
    )


def load_session_digest(session_dir: Path) -> dict | None:
    """Read extractor digest.json if a prior reconstruction left one on disk.

    Frame paths are rewritten to this `session_dir`: the extractor stores
    absolute paths, and a digest restored from S3 was written on another box.
    """
    path = session_dir / ANALYSIS_DIR / "digest.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return None
    except Exception:
        return None
    out_dir = path.parent
    frames = []
    for frame in data.get("frames") or []:
        if not isinstance(frame, dict):
            continue
        name = Path(frame.get("file") or "").name
        if not name:
            continue
        frames.append({**frame, "file": str(out_dir / name)})
    data["frames"] = frames
    return data

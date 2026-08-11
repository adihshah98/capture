"""Reading a session by walking its directory — the pre-Postgres path.

BRIDGE CODE. Everything here reconstructs from files what the `sessions` and
`session_windows` tables now hold, plus four sidecar JSON files that ingest
stopped writing when those columns landed. It survives for one reason: a recording
made before the migration has rows nowhere, so without this it would not replay.

Deletable when no session directory in any pool still contains order.json,
visitor.json, interactions.json or device.json, and every pool's sessions are
represented in Postgres. Until then the read paths fall back here.

Live callers:
  - api/sessions.py — the fallback when sessionstore.get() finds no row
  - backfill.py     — this is how the migration reads what it is migrating

Nothing here should gain features. If you find yourself adding a field, it belongs
in sessionstore and the index instead.
"""

import json
from pathlib import Path

from .attribution import parse_attribution
from .events import event_span, last_active_scan, window_duration
from .paths import SAFE_CHUNK, chunk_timestamp

# Written per window by the recorder before the state moved into session_windows.
WINDOW_STATE_FILE = "window-state.json"

# The four sidecars. Ingest no longer writes any of them.
ORDER_FILE = "order.json"
VISITOR_FILE = "visitor.json"
INTERACTIONS_FILE = "interactions.json"
DEVICE_FILE = "device.json"


def chunk_files(session_dir: Path) -> list:
    return sorted(
        (c for c in session_dir.glob("*.json") if SAFE_CHUNK.match(c.name)),
        key=lambda c: chunk_timestamp(c.name),
    )


def session_states(entry: Path) -> tuple:
    """Rebuild per-window spans from the chunks themselves, so a session that
    predates window-state.json still resolves. Returns (states, first, last, active)."""
    first = last = None
    active = 0
    states: dict = {}
    for chunk in chunk_files(entry):
        try:
            data = json.loads(chunk.read_text())
        except Exception:
            continue
        events = data.get("events") or []
        span = event_span(events)
        if not span:
            continue
        first = span[0] if first is None else min(first, span[0])
        last = span[1] if last is None else max(last, span[1])
        state = states.setdefault(str(data.get("window_id") or "default"), {})
        chunk_active = last_active_scan(events, state)
        if chunk_active:
            active = max(active, chunk_active)
            state["active"] = max(state.get("active", 0), chunk_active)
        state["first"] = min(state.get("first", span[0]), span[0])
        state["last"] = max(state.get("last", span[1]), span[1])
        state["bytes"] = state.get("bytes", 0) + chunk.stat().st_size
    return states, first, last, active


def session_info(entry: Path) -> dict | None:
    """The same shape sessionstore.get() returns, rebuilt from disk. None when the
    directory holds no chunks."""
    chunks = chunk_files(entry)
    if not chunks:
        return None
    states, first, last, active = session_states(entry)
    info = {
        "session_id": entry.name,
        "first_seen": first if first is not None else chunk_timestamp(chunks[0].name),
        "last_seen": last if last is not None else chunk_timestamp(chunks[-1].name),
        "last_active": active,
        "duration": window_duration(states),
        "windows": len(states) or 1,
        "chunk_count": len(chunks),
        "bytes": sum(c.stat().st_size for c in chunks),
    }
    for key, name in (
        ("order", ORDER_FILE),
        ("interactions", INTERACTIONS_FILE),
        ("device", DEVICE_FILE),
    ):
        path = entry / name
        if path.is_file():
            try:
                value = json.loads(path.read_text())
                if isinstance(value, dict):
                    info[key] = value
            except Exception:
                pass
    meta_path = entry / VISITOR_FILE
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text())
            info.update({k: meta[k] for k in ("visitor_id", "url") if isinstance(meta.get(k), str)})
        except Exception:
            pass
    attribution = parse_attribution(info.get("url"))
    if attribution:
        info["attribution"] = attribution
    return info

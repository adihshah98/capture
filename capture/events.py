"""Facts derived from a batch of rrweb events.

Pure functions over the event list — no storage, no database, no request. Shared
by ingest (which folds them into the session row) and the session reader (which
rebuilds them from chunks for recordings that predate the columns).

These are the fast tier's natural home: nothing here needs Postgres.
"""

# rrweb IncrementalSource values that count as the visitor doing something.
# Mirrors the player's buildSegments rules — the two must agree, or the replay
# scrubber shows active time the index does not.
ACTIVE_SOURCES = {1, 2, 3, 4, 5, 6, 7, 12}

# A FullSnapshot this soon after a navigation is part of the navigation, not
# separate activity.
SNAPSHOT_META_WINDOW_MS = 500


def read_device(events: list) -> dict | None:
    """The viewport from the first meta event that carries one.

    A batch with no type-4 event yields None, and the session's device stays
    NULL — which device_class() then refuses to match against any device filter.
    That is why the first chunk matters and later ones cannot correct it.
    """
    for event in events:
        if not isinstance(event, dict) or event.get("type") != 4:
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        width, height = data.get("width"), data.get("height")
        if isinstance(width, int) and isinstance(height, int) and width > 0:
            return {"width": width, "height": height}
    return None


def count_clicks(events: list) -> tuple:
    """This batch's clicks only. The running totals accumulate in SQL, where an
    increment cannot be lost between a read and a write."""
    clicks = rage = 0
    for event in events:
        if not isinstance(event, dict) or event.get("type") != 5:
            continue
        data = event.get("data")
        if not isinstance(data, dict) or data.get("tag") != "capture-click":
            continue
        clicks += 1
        payload = data.get("payload")
        if isinstance(payload, dict) and payload.get("rage"):
            rage += 1
    return clicks, rage


def last_active_scan(events: list, state: dict) -> int | None:
    """Mirror the player's buildSegments activity rules; `state` carries the
    per-window href/nav position across chunks."""
    nav = state.get("nav", -(10**15))
    last = None
    for e in events:
        if not isinstance(e, dict):
            continue
        ts = e.get("timestamp")
        if not isinstance(ts, (int, float)):
            continue
        kind = e.get("type")
        payload = e.get("data")
        data = payload if isinstance(payload, dict) else {}
        active = False
        if kind == 4:
            href = data.get("href")
            if href != state.get("href"):
                state["href"] = href
                nav = ts
                active = True
        elif kind == 2:
            active = ts - nav <= SNAPSHOT_META_WINDOW_MS
        elif kind == 3:
            active = data.get("source") in ACTIVE_SOURCES
        if active:
            last = ts
    state["nav"] = nav
    return int(last) if last is not None else None


def event_span(events: list) -> tuple | None:
    ts = [
        e["timestamp"]
        for e in events
        if isinstance(e, dict) and isinstance(e.get("timestamp"), (int, float))
    ]
    return (int(min(ts)), int(max(ts))) if ts else None


def window_duration(states: dict) -> int | None:
    """Clamped span of the primary (biggest) window — the one the player
    opens by default."""
    spans = [w for w in states.values() if isinstance(w.get("first"), (int, float))]
    if not spans:
        return None
    primary = max(spans, key=lambda w: w.get("bytes", 0))
    end = primary["last"]
    if primary.get("active"):
        end = min(end, primary["active"] + 1500)
    return max(int(end - primary["first"]), 0)

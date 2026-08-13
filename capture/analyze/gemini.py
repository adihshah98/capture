"""Gemini sync and Batch I/O for session write-ups.

Owns the paid edge: interactive `generate_content`, Batch submit/wait/parse/
cancel, and the request assembly that turns a digest + frames into SDK parts.
Orchestrators import this module and call through it so tests can
`mock.patch("capture.analyze.gemini.call_gemini", ...)` at the definition site.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

from .. import attribution
from . import prompts, runtime
from . import store as analyze_store
from .slices import OUTCOME_LABELS, outcome_class, page_path
SYSTEM_PROMPT = prompts.load("session.md")


def _seconds(ms) -> float:
    return round((ms or 0) / 1000, 1)


def _clock(ms) -> str:
    """MM:SS, the same vocabulary the frame labels use."""
    total = int((ms or 0) // 1000)
    return f"{total // 60:02d}:{total % 60:02d}"


# One parser, in capture/attribution.py, shared with the indexer. It used to be
# a twin of server.parse_attribution kept in step by a comment; they diverged.
# `{}` rather than `None` here — this feeds prompt metadata, which is built by
# assignment and never asks whether the key is absent or empty.
_session_attribution = attribution.parse


def session_metadata(project_id: str, session_id: str) -> dict:
    """How this visitor arrived and how the session ended.

    The digest describes what happened on screen and says nothing about where
    the visitor came from, so a write-up built from it alone cannot tie
    behaviour back to the campaign that bought the click. The stored entry URL
    keeps its query string intact — the only place the UTMs survive, since
    page_path() drops them and the digest's `urls` are post-navigation.

    Only fields actually present are returned: a key missing here means
    unknown, and the prompt says so, rather than the model reading an empty
    string as evidence of direct traffic.
    """
    meta: dict = {}

    try:
        entry, order = analyze_store.session_facts(
            analyze_store.connect(), project_id, session_id
        )
    except Exception as exc:
        print(f"session metadata unavailable for {session_id}: {exc}")
        entry, order = None, None

    if entry:
        meta["entry_url"] = str(entry)[:512]
        landing = page_path(entry)
        if landing:
            meta["landing_page"] = landing
        attribution = _session_attribution(meta["entry_url"])
        if attribution:
            meta["attribution"] = attribution
    meta["outcome"] = OUTCOME_LABELS[outcome_class(order)]
    if order:
        for key in ("order_value", "currency", "products"):
            if order.get(key) not in (None, "", [], {}):
                meta[key] = order[key]
    return meta


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


def _usable_frames(frames: list) -> list:
    return [f for f in frames if Path(f.get("file") or "").is_file()]


def _session_model_parts(digest: dict, frames: list, meta: dict | None = None):
    from google.genai import types

    usable = _usable_frames(frames)
    parts = []
    if meta:
        parts.append(
            types.Part.from_text(
                text="Session metadata — how this visitor arrived and how the "
                "session ended. Any field not listed here is UNKNOWN: say so "
                "rather than guessing, and never treat a missing campaign as "
                "proof the visit was direct.\n\n" + json.dumps(meta, indent=2)
            )
        )
    parts += [
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
            types.Part.from_bytes(data=Path(frame["file"]).read_bytes(), mime_type="image/jpeg")
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


def build_inlined_session_request(
    session_id: str, digest: dict, frames: list, meta: dict | None = None
):
    """One Batch API request for a session write-up, keyed by session_id."""
    from google.genai import types

    return types.InlinedRequest(
        contents=[types.Content(role="user", parts=_session_model_parts(digest, frames, meta))],
        metadata={"session_id": session_id},
        config=_session_model_config(),
    )


# SDK-only GenerateContentConfig fields. They are not GenerationConfig, and
# leaving them in the JSONL is a 400.
_SDK_CONFIG_KEYS = frozenset(
    {
        "httpOptions",
        "http_options",
        "shouldReturnHttpResponse",
        "should_return_http_response",
    }
)


def _batch_file_request(req) -> tuple[str, dict]:
    """One JSONL record in the Files-API shape the batch parser accepts.

    `InlinedRequest.model_dump()` is the SDK's inline-batch wrapper: a `config`
    object and snake_case names. The file parser wants a GenerateContentRequest
    — `systemInstruction` + `generationConfig`, camelCase. Dumping the wrapper
    400'd every submit and the cohort fell back to sync Gemini at full price.
    """
    dumped = req.model_dump(mode="json", exclude_none=True, by_alias=True)
    meta = dumped.pop("metadata", None) or {}
    key = meta.get("session_id") or meta.get("sessionId") or ""
    config = dumped.pop("config", None) or {}
    system = config.pop("systemInstruction", None)
    if system is None:
        system = config.pop("system_instruction", None)
    for sdk_key in _SDK_CONFIG_KEYS:
        config.pop(sdk_key, None)
    if system is not None:
        dumped["systemInstruction"] = system
    if config:
        dumped["generationConfig"] = config
    if not key:
        raise ValueError("batch request is missing session_id metadata")
    return key, dumped


async def call_gemini(digest: dict, frames: list, meta: dict | None = None) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=runtime.GEMINI_API_KEY)
    response = await client.aio.models.generate_content(
        model=runtime.GEMINI_MODEL,
        contents=[types.Content(role="user", parts=_session_model_parts(digest, frames, meta))],
        config=_session_model_config(),
    )
    text = (response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty analysis")
    return text


def _batch_jsonl_path(work_dir: Path | None = None) -> Path:
    """A private, unshared file for one batch's JSONL.

    Disk-backed rather than /tmp: on a small box /tmp is tmpfs, and this holds a
    whole cohort's frames.

    `mkstemp` rather than a timestamped name. The name used to be
    `capture-batch-<ms>.jsonl`, so two cohorts submitting in the same millisecond
    got the same path — and since the caller unlinks in a `finally`, the first to
    finish deleted the other's payload out from under an in-flight upload. It
    also creates 0600, which matters more than the collision: the default base is
    a world-readable shared directory and these lines carry session frames and
    visitor metadata. Set explicitly rather than relying on the default, since
    that is the property being depended on.
    """
    base = work_dir if work_dir is not None else Path("/var/tmp")
    base.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="capture-batch-", suffix=".jsonl", dir=base)
    os.close(fd)
    path = Path(name)
    path.chmod(0o600)
    return path


async def submit_prepared_gemini_batch(
    prepared: list,
    *,
    display_name: str,
    work_dir: Path | None = None,
    project_id: str | None = None,
) -> str:
    """Create a Batch job from (entry, digest) pairs, one session at a time.

    Streams JSONL to disk so peak RAM is roughly one session's frames, not the
    whole cohort. Always uses the Files API — at cohort scale the inline path
    is both oversized and an OOM hazard while estimating size.

    `project_id` is what lets each request carry its session's campaign
    metadata; a batch submitted without it still works, minus that context.
    """
    from google import genai
    from google.genai import types

    if not prepared:
        raise ValueError("batch requires at least one request")

    client = genai.Client(api_key=runtime.GEMINI_API_KEY)
    config = types.CreateBatchJobConfig(display_name=display_name[:128])
    tmp = _batch_jsonl_path(work_dir)
    try:
        with tmp.open("w") as out:
            for entry, digest in prepared:
                sid = entry["session_id"]
                meta = (
                    session_metadata(project_id, sid) if project_id is not None else None
                )
                req = build_inlined_session_request(sid, digest, digest.get("frames") or [], meta)
                key, payload = _batch_file_request(req)
                del req
                out.write(json.dumps({"key": key, "request": payload}, separators=(",", ":")))
                out.write("\n")
                del payload
        # `jsonl` becomes application/jsonl, which the Files API 400s. JSONL
        # content with application/json is the workaround that actually uploads.
        uploaded = await client.aio.files.upload(
            file=str(tmp),
            config=types.UploadFileConfig(mime_type="application/json"),
        )
    finally:
        tmp.unlink(missing_ok=True)

    job = await client.aio.batches.create(
        model=runtime.GEMINI_MODEL,
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

    client = genai.Client(api_key=runtime.GEMINI_API_KEY)
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
            out[sid] = {"error": err.get("message") if isinstance(err, dict) else str(err)}
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


class BatchStillRunning(RuntimeError):
    """Gave up watching a batch that is, as far as we know, still alive.

    Distinct from a batch that failed: the job name remains valid and its
    results are still coming, so the caller must keep the name on disk and let
    a resume collect them. Re-running the sessions instead would pay for the
    same work twice.
    """


async def wait_gemini_batch(
    job_name: str,
    *,
    poll_s: float | None = None,
    max_wait_s: float | None = None,
):
    """Poll until the batch reaches a terminal state; return the final job.

    Raises BatchStillRunning if the deadline passes or polling keeps failing —
    in both cases the job itself is untouched and can be picked up later.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=runtime.GEMINI_API_KEY)
    interval = runtime.ANALYZE_BATCH_POLL_S if poll_s is None else poll_s
    limit = runtime.ANALYZE_BATCH_MAX_WAIT_S if max_wait_s is None else max_wait_s
    deadline = None if limit <= 0 else time.monotonic() + limit
    terminal = {
        types.JobState.JOB_STATE_SUCCEEDED,
        types.JobState.JOB_STATE_FAILED,
        types.JobState.JOB_STATE_CANCELLED,
        types.JobState.JOB_STATE_EXPIRED,
        types.JobState.JOB_STATE_PARTIALLY_SUCCEEDED,
    }
    terminal_names = {s.name for s in terminal}
    errors = 0
    while True:
        try:
            job = await client.aio.batches.get(name=job_name)
            errors = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A failed poll says nothing about the job, so treat it as a blip
            # until it stops looking like one.
            errors += 1
            if errors >= runtime.ANALYZE_BATCH_POLL_MAX_ERRORS:
                raise BatchStillRunning(
                    f"could not read batch {job_name} after {errors} attempts: {exc}"
                ) from exc
            print(f"batch poll {job_name} failed ({exc}); retry {errors}")
        else:
            # Compare by name first: _batch_state_name handles every shape the
            # SDK returns, while `state in terminal` raises on anything
            # unhashable and would defeat that fallback by going first.
            if _batch_state_name(job) in terminal_names:
                return job
            try:
                if getattr(job, "state", None) in terminal:
                    return job
            except TypeError:
                pass
        if deadline is not None and time.monotonic() >= deadline:
            raise BatchStillRunning(f"batch {job_name} still running after {limit:.0f}s")
        await asyncio.sleep(max(1.0, interval))


async def cancel_gemini_batch(job_name: str) -> None:
    """Best-effort cancel of an in-flight Batch job."""
    if not job_name:
        return
    from google import genai

    client = genai.Client(api_key=runtime.GEMINI_API_KEY)
    try:
        await client.aio.batches.cancel(name=job_name)
    except Exception as exc:
        print(f"batch cancel {job_name}: {exc}")

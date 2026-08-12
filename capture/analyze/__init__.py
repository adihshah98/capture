"""Session analysis: replay the session headlessly, then ask Gemini about it.

The extractor drives the same CapturePlayer the replay UI uses, so animations
are frozen and recorded scroll offsets are re-applied exactly as they are on
screen. Stepping that player yields a frame per second of activity — the page
as the visitor saw it change, rather than a single end-state snapshot.

Also owns cohorts: sample latest purchased + non-purchase sessions (or any
matching set, up to 500 total), persist each sample without touching prior
cohorts, reconstruct pages locally, then run write-ups through the Gemini Batch
API (interactive generate_content remains for single-session Analyze). A cohort
is immutable once sampled, so a "cut" across it can be analysed once and cached
against it (`refresh=1` busts that cache).

Cohort state lives in Postgres (`0003_cohorts.sql`); the `cohorts/<id>.json`
layout this module used to write is read only by the migration in store.py.
Sampling defaults are in spec.py, not here.
"""

import asyncio
import hashlib
import json
import re
import shutil
import time
from dataclasses import replace
from pathlib import Path

from ..paths import SAFE_CHUNK

# Submodules kept reachable as `analyze.<name>` for callers that navigate to
# them that way. `as` rather than a bare import because these are re-exports,
# not uses: this file calls through the names below, not through the modules.
from . import cohort_state as cohort_state
from . import gemini, prompts, runtime, spec
from . import slices as slices
from . import stats as stats
from . import storage as storage
from . import store as analyze_store

# A cohort's status, projected from its session rows rather than read back. The
# state machine and the view/persist split live in cohort_state.py; re-exported
# because the port and the suite reach them off the package. `_undeclared_
# transitions` is a test hook the suite clears in place, so it must stay the same
# list object here — a copy would silently stop recording.
from .cohort_state import (
    COHORT_STATES,
    COHORT_TRANSITIONS,
    COHORT_VIEW_ONLY_FIELDS,
    COHORT_VIEW_ONLY_SESSION_FIELDS,
    SESSION_STALE,
    CohortState,
    attach_cohort_insights,
    cohort_transition,
    enrich_cohort_status,
    persistable_cohort,
    project_cohort_status,
    standout_line,
)
from .cohort_state import (
    _undeclared_transitions as _undeclared_transitions,
)

# The model's cut reply, coerced into the documented shape — including the guard
# that drops a share whose denominator is not this group's size.
from .cutfmt import CUT_SECTIONS, empty_cut_analysis, normalize_cut_analysis

# How a cohort describes itself: the selection sentence under it in the UI, and
# the auto-generated display name. Pure string generation over a normalised slice.
from .labels import _auto_cohort_label, clean_cohort_label, cohort_selection_text

# Cohort membership and the facet picker: the only SQL in the package.
from .sampling import facets_from_rows, pool_facets, sample_cohort_rows

# The slice language: pure predicates over dicts, shared by the sampler, the cut
# cache key and the UI labels.
from .slices import (
    DEVICE_VALUES,
    OUTCOME_LABELS,
    OUTCOME_VALUES,
    device_class,
    normalize_slice,
    outcome_class,
    page_path,
    row_matches_slice,
    slice_label,
)

# Measured splits: the model invents shares when asked to count, so the numbers
# in the cut prompt are computed in Python and handed over as exact. The gates
# and the cell builder are stats.py's own business, and tests reach them there.
from .stats import cross_tab_block

# Result and cohort persistence, addressed by pool path and session directory.
# Re-exported rather than re-implemented: `capture/analysis_port.py` and the
# suite reach these off the package, and ANALYSIS_FILE in particular is read by
# the port at import time. None of them is a patchable name, so binding by value
# here is safe — see the patching contract at the foot of this file.
from .storage import (
    ANALYSIS_DIR,
    ANALYSIS_FILE,
    COHORT_FILE,
    COHORTS_DIR,
    CUTS_SUFFIX,
    PROJECTS_DIR,
    SAFE_COHORT_ID,
    analysis_paths,
    clear_analysis,
    clear_cut,
    clear_job,
    cohort_path,
    cohort_store,
    cohorts_dir,
    cuts_path,
    data_root_for,
    forget_analyses,
    forget_pool_cohorts,
    forget_session,
    latest_cohort,
    list_cohorts,
    mark_cohort_session,
    migrate_legacy_cohort,
    pool_key,
    read_analysis,
    read_cohort,
    read_cut,
    read_cuts,
    read_job,
    safe_cohort_id,
    write_analysis,
    write_cohort,
    write_cut,
    write_job,
)
from .storage import (
    _is_project_pool as _is_project_pool,
)
from .storage import (
    _migrate_cohorts_once as _migrate_cohorts_once,
)
from .storage import (
    _migrate_lock as _migrate_lock,
)
from .storage import (
    _migrated as _migrated,
)
from .storage import (
    _read_cohort_file as _read_cohort_file,
)
from .storage import (
    _session_ref as _session_ref,
)

# What the package exports. Declared because most of the names above are
# re-exports this file never calls itself -- `capture/analysis_port.py` and the
# suite reach them off the package -- and without a declaration a linter reads
# every one of them as an unused import and offers to delete the package's map
# of itself.
#
# Not an access control: `analyze.<anything>` still resolves, which is what the
# patching contract at the foot of this file depends on. It states intent, and
# `tests/server/test_patch_contract.py` is what actually holds the surface still.
__all__ = [
    # Configuration and runtime.
    "ANALYSIS_ENABLED",
    "ANALYZE_BATCH",
    "ANALYZE_BATCH_MAX_WAIT_S",
    "ANALYZE_BATCH_POLL_MAX_ERRORS",
    "ANALYZE_BATCH_POLL_S",
    "ANALYZE_EXTRACT_CONCURRENCY",
    "ANALYZE_FPS",
    "ANALYZE_MAX_FRAMES",
    "ANALYZE_MODEL_CONCURRENCY",
    "ANALYZE_TIMEOUT_S",
    "BASE_DIR",
    "EXTRACTOR",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "REPLAYER_BUNDLE",
    "runtime_present",
    # Sampling vocabulary, aliased from spec.py under the older names.
    "COHORT_CANDIDATE_CAP",
    "COHORT_DEFAULT_ALL_N",
    "COHORT_DEFAULT_PURCHASED_N",
    "COHORT_MAX_SESSIONS",
    "COHORT_MIN_DURATION_MS",
    "COHORT_ORDERS",
    "COHORT_ORDER_EARLIEST",
    "COHORT_ORDER_LATEST",
    "COHORT_URL_CONTAINS",
    # Prompts.
    "CHAT_PROMPT",
    "CUT_PROMPT",
    "SYSTEM_PROMPT",
    "prompt_version",
    # Persistence (storage.py).
    "ANALYSIS_DIR",
    "ANALYSIS_FILE",
    "COHORTS_DIR",
    "COHORT_FILE",
    "CUTS_SUFFIX",
    "PROJECTS_DIR",
    "SAFE_COHORT_ID",
    "analysis_paths",
    "clear_analysis",
    "clear_cut",
    "clear_job",
    "cohort_path",
    "cohort_store",
    "cohorts_dir",
    "cuts_path",
    "data_root_for",
    "forget_analyses",
    "forget_pool_cohorts",
    "forget_session",
    "latest_cohort",
    "list_cohorts",
    "mark_cohort_session",
    "migrate_legacy_cohort",
    "pool_key",
    "read_analysis",
    "read_cohort",
    "read_cut",
    "read_cuts",
    "read_job",
    "safe_cohort_id",
    "write_analysis",
    "write_cohort",
    "write_cut",
    "write_job",
    # Cohort status (cohort_state.py).
    "COHORT_STATES",
    "COHORT_TRANSITIONS",
    "COHORT_VIEW_ONLY_FIELDS",
    "COHORT_VIEW_ONLY_SESSION_FIELDS",
    "CohortState",
    "SESSION_STALE",
    "attach_cohort_insights",
    "cohort_transition",
    "enrich_cohort_status",
    "persistable_cohort",
    "project_cohort_status",
    "standout_line",
    # The slice language (slices.py) and the measured splits (stats.py).
    "DEVICE_VALUES",
    "OUTCOME_LABELS",
    "OUTCOME_VALUES",
    "cross_tab_block",
    "device_class",
    "normalize_slice",
    "outcome_class",
    "page_path",
    "row_matches_slice",
    "slice_label",
    # Cuts and chat.
    "CHAT_MAX_MESSAGE_CHARS",
    "CHAT_MAX_TURNS",
    "CUT_SECTIONS",
    "build_cut_analysis",
    "call_gemini_cut",
    "chat_context",
    "clean_chat_messages",
    "cut_context",
    "cut_key",
    "empty_cut_analysis",
    "normalize_cut_analysis",
    "select_rows_by_cut",
    "stream_gemini_chat",
    # Cohort lifecycle (sampling.py, labels.py).
    "build_cohort",
    "clean_cohort_label",
    "cohort_selection_text",
    "delete_cohort",
    "facets_from_rows",
    "pool_facets",
    "rename_cohort",
    "sample_cohort_rows",
    # Extraction and the session write-up.
    "analyze_session",
    "build_inlined_session_request",
    "call_gemini",
    "extract_session_digest",
    "load_session_digest",
    "prompt_digest",
    "run_extractor",
    "session_chunk_names",
    "session_metadata",
    "write_analysis_result",
    # Batch.
    "BatchStillRunning",
    "apply_cohort_batch",
    "cancel_gemini_batch",
    "parse_batch_results",
    "submit_prepared_gemini_batch",
    "wait_gemini_batch",
    # Orchestration.
    "cohorts_needing_batch_resume",
    "iter_session_pools",
    "mark_cohort_paused",
    "resume_cohort_batch",
    "run_cohort_job",
]

# Config, path anchors, and the shared extract/model semaphores live in
# runtime.py. Re-exported below so `analyze.X` and analysis_port keep working;
# call sites in this file go through the `runtime` module object so test patches
# against `capture.analyze.runtime` reach the reader.
_gates = runtime._gates
runtime_present = runtime.runtime_present

# Mutable / rebound runtime names are forwarded via __getattr__ rather than
# copied at import: a from-import of ANALYSIS_ENABLED would freeze the bool, and
# analyze._gate_loop = None would set an unrelated attribute while _gates kept
# reading runtime._gate_loop. Tests that rebind these assign on analyze.runtime.
_RUNTIME_EXPORTS = frozenset(
    {
        "ANALYSIS_ENABLED",
        "ANALYZE_BATCH",
        "ANALYZE_BATCH_MAX_WAIT_S",
        "ANALYZE_BATCH_POLL_MAX_ERRORS",
        "ANALYZE_BATCH_POLL_S",
        "ANALYZE_EXTRACT_CONCURRENCY",
        "ANALYZE_FPS",
        "ANALYZE_MAX_FRAMES",
        "ANALYZE_MODEL_CONCURRENCY",
        "ANALYZE_TIMEOUT_S",
        "BASE_DIR",
        "EXTRACTOR",
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "REPLAYER_BUNDLE",
        "_extract_gate",
        "_gate_loop",
        "_model_gate",
    }
)

# Gemini I/O and its request-assembly helpers live in gemini.py. Forwarded the
# same way as runtime so `analyze.call_gemini` stays the public name while
# orchestration and tests resolve through `gemini.<name>` / mock.patch.
_GEMINI_EXPORTS = frozenset(
    {
        "BatchStillRunning",
        "SYSTEM_PROMPT",
        "_batch_jsonl_path",
        "_batch_response_text",
        "_batch_state_name",
        "_clock",
        "_seconds",
        "_session_attribution",
        "_session_model_config",
        "_session_model_parts",
        "_usable_frames",
        "build_inlined_session_request",
        "call_gemini",
        "cancel_gemini_batch",
        "parse_batch_results",
        "prompt_digest",
        "session_metadata",
        "submit_prepared_gemini_batch",
        "wait_gemini_batch",
    }
)


def __getattr__(name: str):
    if name in _RUNTIME_EXPORTS:
        return getattr(runtime, name)
    if name in _GEMINI_EXPORTS:
        return getattr(gemini, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# The sampling vocabulary lives in spec.py. Aliased under the older names so the
# sampler and the port keep reading the way they always have.
COHORT_DEFAULT_PURCHASED_N = spec.DEFAULT_PURCHASED_N
COHORT_DEFAULT_ALL_N = spec.DEFAULT_ALL_N
COHORT_MAX_SESSIONS = spec.MAX_SESSIONS
COHORT_MIN_DURATION_MS = spec.MIN_DURATION_MS
COHORT_URL_CONTAINS = spec.URL_CONTAINS
COHORT_CANDIDATE_CAP = spec.CANDIDATE_CAP
COHORT_ORDER_LATEST = spec.ORDER_LATEST
COHORT_ORDER_EARLIEST = spec.ORDER_EARLIEST
COHORT_ORDERS = spec.ORDERS


def prompt_version(text: str) -> str:
    """Short content hash of a prompt, for cache keys.

    Content-addressed rather than a hand-maintained constant: a version number
    someone has to remember to bump is exactly the mechanism that rots. Eight
    hex characters is ample to separate revisions of one prompt — this is cache
    invalidation, not integrity.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def cut_key(slice_spec: dict | None) -> str:
    """Stable cache key for a cut: the filter plus the prompt that answers it.

    A cut's answer is a function of which sessions were selected *and* what the
    model was asked. Keying on the filter alone meant a reworded CUT_PROMPT
    kept serving answers generated by the old one, with no invalidation path
    and no signal — the response still said `cached: true`. Since prompt work
    is most of this module's history, that was the common case, not an edge.

    The model is deliberately not in the key. Switching GEMINI_MODEL is rare
    and intentional, and every cached cut already records the `model` that
    produced it, so it stays traceable without invalidating every cohort's
    cache at once on an env-var change.

    Cuts cached before this carried no prompt hash, so their keys no longer
    match and they are simply re-asked once.
    """
    return json.dumps(
        {
            "filter": normalize_slice(slice_spec),
            "prompt": prompt_version(CUT_PROMPT),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def select_rows_by_cut(rows: list, slice_spec: dict | None) -> tuple:
    """(matching_rows, spec). An empty filter selects the whole cohort."""
    spec = normalize_slice(slice_spec)
    return [row for row in rows if row_matches_slice(row, spec)], spec


CUT_PROMPT = prompts.load("cut.md")


def _cut_side_text(rows: list, label: str) -> tuple:
    """(text, citations) — the group's write-ups, numbered for citation.

    The 1-based number in each header is what the model cites as S<n>. The
    `citations` map returned alongside is that same numbering as data —
    {"1": session_id, ...} — built in this loop, so the numbers the model was
    shown and the ids the UI resolves cannot drift apart. Callers must store
    what they get back rather than rebuilding it from the row list.
    """
    if not rows:
        return f"### {label}\n(no sessions in this group)\n", {}
    citations = {}
    parts = [f"### {label} — {len(rows)} session(s)\n"]
    for i, row in enumerate(rows, 1):
        citations[str(i)] = row.get("session_id")
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
    return "\n".join(parts), citations


def _given_facts(spec: dict, n: int) -> str:
    """Spell out what the filter already guarantees, so it is not 'found'.

    `n` is the size of the filtered group. Every premise below states it, so
    the only session count in the model's context is the right one: given a
    bare "every session in this group", the model has been seen to attach the
    cohort-wide total instead ("All 46 sessions landed on /x" for a group of 9).
    """
    lines = [
        f"- This group contains exactly {n} session(s). {n} is the ONLY total "
        f"you may use. Every share you write is out of {n} — never out of any "
        f"larger number, and never out of the whole cohort."
    ]

    if spec.get("outcome"):
        joined = " or ".join(OUTCOME_LABELS.get(v, v) for v in spec["outcome"])
        lines.append(
            f"- All {n} session(s) in this group {joined}. Do not report this, "
            f"or any step it entails (clicking buy, reaching checkout, "
            f"converting), as a finding."
        )
    if spec.get("device"):
        lines.append(
            f"- All {n} session(s) in this group were on "
            f"{' or '.join(spec['device'])}. Do not report the device itself "
            f"as a finding."
        )
    if spec.get("page"):
        lines.append(
            f"- All {n} session(s) in this group landed on {spec['page']}. Do "
            f"not report the landing page itself as a finding."
        )
    for key in ("utm_source", "utm_medium", "utm_campaign", "utm_content"):
        if spec.get(key):
            lines.append(
                f"- All {n} session(s) in this group came from "
                f"{key}={spec[key]}. Do not report the traffic source as a "
                f"finding."
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
    # len(lines) > 1 means the filter actually pinned a field: the count line
    # is always present and pins nothing on its own.
    if len(lines) > 1 and free:
        lines.append(
            "- These are fixed for every session here, so they cannot separate "
            "one cluster from another. Cluster on " + ", ".join(free) + " and "
            "on what the sessions actually did on the page."
        )

    return "## Already known — never report these as findings\n\n" + "\n".join(lines)


def cut_context(rows: list, label: str, given: str = "", spec: dict | None = None) -> tuple:
    """(header, side_text, citations) — the evidence a group's questions run on.

    Shared by the cut write-up and the chat, so the two always reason over the
    same numbered write-ups, the same "already known" premises and the same
    measured splits. Anything that changes here changes both, which is the
    point: a chat that saw a different context than the cut it sits under
    would answer questions about a group the user is not looking at.
    """
    header = f"This group is: {label}\n"
    if given:
        header += "\n" + given + "\n"
    measured = cross_tab_block(rows, spec)
    if measured:
        header += "\n" + measured + "\n"
    header += "\nThe write-ups for this group follow."
    side_text, citations = _cut_side_text(rows, "SESSIONS")
    return header, side_text, citations


async def call_gemini_cut(
    rows: list,
    label: str,
    given: str = "",
    spec: dict | None = None,
) -> tuple:
    """(analysis, citations) — one text-only pass over the group's write-ups.

    The citation map comes back from the same call that numbered the write-ups,
    so what the caller stores is exactly what the model was shown.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=runtime.GEMINI_API_KEY)
    header, side_text, citations = cut_context(rows, label, given, spec)
    parts = [
        types.Part.from_text(text=header),
        types.Part.from_text(text=side_text),
    ]
    response = await client.aio.models.generate_content(
        model=runtime.GEMINI_MODEL,
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
    analysis = normalize_cut_analysis(_parse_json_object(response.text or ""), len(rows))
    return analysis, citations


async def build_cut_analysis(rows: list, slice_spec: dict | None) -> dict:
    """Filter the cohort to one group and ask the model what it did."""
    if not runtime.ANALYSIS_ENABLED:
        raise RuntimeError("analysis is not configured — set GEMINI_API_KEY")

    selected, spec = select_rows_by_cut(rows, slice_spec)
    label = slice_label(spec) if spec else "all sessions"

    base = {
        "cut": cut_key(slice_spec),
        "label": label,
        "n": len(selected),
        "session_ids": [r.get("session_id") for r in selected],
        "model": runtime.GEMINI_MODEL,
        "created_at": int(time.time() * 1000),
    }

    # Nothing to read — say so rather than spending a call on an empty group.
    if not selected:
        empty = empty_cut_analysis()
        empty["headline"] = f"No analyzed sessions match {label} in this cohort."
        return {**base, "analysis": empty, "citations": {}}

    given = _given_facts(spec, len(selected))
    _, model_gate = runtime._gates()
    async with model_gate:
        analysis, citations = await call_gemini_cut(selected, label, given, spec)
    # Stored, not derived: this is the numbering the model actually saw, so a
    # later change to row ordering cannot repoint the links in this cut.
    return {**base, "analysis": analysis, "citations": citations}


CHAT_PROMPT = prompts.load("chat.md")

# A turn is one call, so history is the only thing that grows. Cap it: a long
# thread would otherwise re-send the whole write-up block plus an unbounded
# transcript on every question.
CHAT_MAX_TURNS = 12
CHAT_MAX_MESSAGE_CHARS = 4000


def clean_chat_messages(messages) -> list:
    """Trim history to the recent turns, as {role, content} with role user/model.

    The client owns the transcript, so this is untrusted input: anything that is
    not a usable turn is dropped rather than forwarded to the model.
    """
    if not isinstance(messages, list):
        return []
    clean = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        # The UI speaks in assistant/user; Gemini wants model/user.
        if role in ("assistant", "model"):
            role = "model"
        elif role != "user":
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        clean.append({"role": role, "content": content.strip()[:CHAT_MAX_MESSAGE_CHARS]})
    # Keep the most recent turns; the question is always the last one.
    return clean[-CHAT_MAX_TURNS:]


async def stream_gemini_chat(
    rows: list, label: str, given: str, messages: list, spec: dict | None = None
):
    """Yield answer text chunks for the latest question in `messages`.

    The evidence goes in as the first user turn with a canned acknowledgement
    after it, so the conversation that follows is ordinary chat over a context
    the model has already "read". That keeps the write-up block a stable prefix
    across turns, which is what makes implicit caching possible.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=runtime.GEMINI_API_KEY)
    header, side_text, _ = cut_context(rows, label, given, spec)
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(text=header),
                types.Part.from_text(text=side_text),
            ],
        ),
        types.Content(
            role="model",
            parts=[
                types.Part.from_text(
                    text=(
                        f"I have read the {len(rows)} write-up(s) for this group. "
                        "Ask me anything about them."
                    )
                )
            ],
        ),
    ]
    for message in messages:
        contents.append(
            types.Content(
                role=message["role"],
                parts=[types.Part.from_text(text=message["content"])],
            )
        )

    stream = await client.aio.models.generate_content_stream(
        model=runtime.GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=CHAT_PROMPT,
            # Answers are prose, not five sections of insight objects, so this
            # sits well below the cut's ceiling. Thinking still gets room to
            # count across the write-ups before answering.
            max_output_tokens=4096,
            thinking_config=types.ThinkingConfig(thinking_budget=2048),
            temperature=0.2,
        ),
    )
    async for chunk in stream:
        text = getattr(chunk, "text", None)
        if text:
            yield text


async def chat_context(rows: list, slice_spec: dict | None) -> tuple:
    """(selected_rows, label, given, citations, spec) for a group.

    Mirrors build_cut_analysis's setup so a chat answers about exactly the group
    the cut above it describes. `spec` comes back so the caller can hand it to
    stream_gemini_chat: without it the chat would rebuild its context without
    the measured splits and answer from a thinner view than the cut on screen.
    """
    if not runtime.ANALYSIS_ENABLED:
        raise RuntimeError("analysis is not configured — set GEMINI_API_KEY")
    selected, spec = select_rows_by_cut(rows, slice_spec)
    label = slice_label(spec) if spec else "all sessions"
    given = _given_facts(spec, len(selected))
    _, _, citations = cut_context(selected, label, given, spec)
    return selected, label, given, citations, spec


def build_cohort(
    conn: spec.Connection,
    pool_key: str,
    selection: spec.CohortSpec,
    *,
    label: str | None = None,
) -> dict:
    """Sample a cohort and record how it was sampled.

    The returned dict is the cohort as it goes to storage: its membership, plus
    every field of the selection rule, because a cohort is immutable once
    sampled and the rule is the only record of what it represents.
    """
    now = int(time.time() * 1000)
    selection = replace(
        selection.normalised(),
        slice_spec=normalize_slice(selection.slice_spec),
        slice_b=normalize_slice(selection.slice_b),
    )
    sessions = sample_cohort_rows(conn, pool_key, selection)
    stamp = time.strftime("%b %-d, %-I:%M %p", time.localtime(now / 1000))
    chosen = clean_cohort_label(label) or _auto_cohort_label(selection, stamp=stamp)
    return {
        "id": f"cohort-{now}",
        # Chosen name, or auto label when none was provided.
        "label": chosen,
        "selection": cohort_selection_text(selection),
        "created_at": now,
        "updated_at": now,
        # Sampled only — analysis starts via POST .../analyze.
        "status": "idle",
        # The legacy arms are only meaningful in their own mode; recording them
        # alongside any_n/a_n would make the stored rule ambiguous.
        "purchased_n": selection.purchased_n
        if selection.total == selection.purchased_n + selection.all_n
        else 0,
        "all_n": selection.all_n
        if selection.total == selection.purchased_n + selection.all_n
        else 0,
        "any_n": selection.any_n,
        "a_n": selection.a_n,
        "b_n": selection.b_n,
        "min_duration_ms": selection.min_duration_ms,
        "url_contains": (selection.url_contains or "").strip(),
        "start_ms": selection.start_ms,
        "end_ms": selection.end_ms,
        "order": selection.order,
        "slice": selection.slice_spec,
        "slice_b": selection.slice_b if selection.b_n > 0 else {},
        "sessions": sessions,
    }


def rename_cohort(pool: Path, cohort_id: str, label: str) -> dict | None:
    """Set a cohort's display label. Membership is untouched."""
    cohort = read_cohort(pool, cohort_id)
    if not cohort:
        return None
    clean = clean_cohort_label(label)
    if not clean:
        raise ValueError("label cannot be empty")
    return write_cohort(pool, {**cohort, "label": clean})


def delete_cohort(pool: Path, cohort_id: str) -> bool:
    """Remove a cohort and its cached cuts. Session write-ups on disk stay."""
    try:
        cohort_id = safe_cohort_id(cohort_id)
    except ValueError:
        return False
    conn, key = cohort_store(pool)
    return analyze_store.delete_cohort(conn, key, cohort_id)


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
        # Positional, and ahead of the variadic chunk names.
        str(runtime.ANALYZE_FPS),
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
    session_dir: Path,
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
    write_analysis(session_dir, result)
    clear_job(session_dir)
    return result


def mark_cohort_paused(pool: Path, cohort: dict) -> dict:
    """Persist paused status and return in-flight session rows to queued."""
    for entry in cohort.get("sessions") or []:
        session_dir = pool / entry["session_id"]
        if read_analysis(session_dir):
            entry["status"] = "done"
            entry["analysis"] = ANALYSIS_FILE
            entry.pop("error", None)
            continue
        if (
            entry.get("status") in ("running", "queued")
            or (read_job(session_dir) or {}).get("status") == "running"
        ):
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

    extract_gate, _ = runtime._gates()
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
    if not runtime.ANALYSIS_ENABLED:
        raise RuntimeError("analysis is not configured — set GEMINI_API_KEY")

    if not force:
        cached = read_analysis(session_dir)
        if cached:
            return cached

    digest = await extract_session_digest(session_dir, chunk_names, force=True)
    meta = gemini.session_metadata(session_dir)
    _, model_gate = runtime._gates()
    async with model_gate:
        text = await gemini.call_gemini(digest, digest.get("frames") or [], meta)
    return write_analysis_result(
        session_dir,
        text,
        frame_count=len(digest.get("frames") or []),
        warnings=digest.get("warnings") or [],
    )


def load_session_digest(session_dir: Path) -> dict | None:
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
        # Same rule as the first attempt: a batch we could not collect keeps
        # its name and stays resumable. Letting this raise would strand the
        # cohort as "running" with nothing watching it, since the startup
        # resume task is never awaited.
        if not await _collect_cohort_batch(pool, cohort, prepared, job_name):
            return enrich_cohort_status(pool, read_cohort(pool, cohort["id"]) or cohort)
    live = read_cohort(pool, cohort["id"]) or cohort
    live.pop("batch", None)
    final = enrich_cohort_status(pool, live)
    write_cohort(pool, persistable_cohort(final, live))
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
            write_job(session_dir, "running")
            # One row, not the whole document: these coroutines run
            # concurrently and each owns exactly one session.
            mark_cohort_session(pool, cohort, entry, "running")
            try:
                # Reuse a prior reconstruction unless the caller asked to redo.
                # Without this, a retry after a Batch OOM would wipe digests and
                # spend another hour in Playwright before talking to Gemini.
                if not force:
                    digest = load_session_digest(session_dir)
                    if digest and gemini._usable_frames(digest.get("frames") or []):
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
                message = str(exc) or "analysis failed"
                write_job(session_dir, "error", error=message)
                mark_cohort_session(pool, cohort, entry, "error", error=message)

        await asyncio.gather(*(extract_one(e) for e in pending_entries), return_exceptions=True)

        if not prepared:
            return enrich_cohort_status(pool, read_cohort(pool, cohort["id"]) or cohort)

        use_batch = runtime.ANALYZE_BATCH and runtime.ANALYSIS_ENABLED
        job_name = None
        if use_batch:
            # Only submission falls back to sync. Once a job exists it is
            # billable, so a later failure must keep its name and be resumed
            # rather than re-running every session at interactive prices.
            try:
                job_name = await gemini.submit_prepared_gemini_batch(
                    prepared,
                    display_name=f"capture-{cohort['id']}",
                    work_dir=cohorts_dir(pool),
                    pool=pool,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # No job was created, so nothing has been paid for: falling
                # back keeps a Batch outage from stranding an extracted cohort.
                print(f"cohort batch submit failed ({exc}); falling back to sync Gemini")
                cohort.pop("batch", None)
                write_cohort(pool, cohort)
                await _cohort_sync_model(pool, cohort, prepared)

        if job_name:
            cohort["batch"] = {
                "name": job_name,
                "submitted_at": int(time.time() * 1000),
                "session_ids": [e["session_id"] for e, _ in prepared],
            }
            write_cohort(pool, cohort)
            if not await _collect_cohort_batch(pool, cohort, prepared, job_name):
                # Still in flight and no longer being watched. The name stays
                # on disk so startup resume — or a Start from the UI — picks
                # the same job back up.
                return enrich_cohort_status(pool, read_cohort(pool, cohort["id"]) or cohort)
        elif not use_batch:
            await _cohort_sync_model(pool, cohort, prepared)

        live = read_cohort(pool, cohort["id"]) or cohort
        live.pop("batch", None)
        final = enrich_cohort_status(pool, live)
        write_cohort(pool, persistable_cohort(final, live))
        return final
    except asyncio.CancelledError:
        live = read_cohort(pool, cohort["id"]) or cohort
        batch_name = (live.get("batch") or {}).get("name")
        if batch_name:
            await gemini.cancel_gemini_batch(batch_name)
        mark_cohort_paused(pool, live)
        raise


async def _collect_cohort_batch(pool: Path, cohort: dict, prepared: list, job_name: str) -> bool:
    """Apply a submitted batch. False when it is still in flight.

    Everything past submission is recoverable: the job exists and has been paid
    for, so a batch we simply stopped watching must leave its name on disk for
    a resume. Only a batch that genuinely failed marks its sessions errored.
    """
    try:
        await apply_cohort_batch(pool, cohort, prepared, job_name=job_name)
        return True
    except asyncio.CancelledError:
        raise
    except gemini.BatchStillRunning as exc:
        print(f"cohort batch {job_name} not collected ({exc}); leaving it to resume")
        _mark_prepared_still_running(pool, cohort, prepared)
        return False
    except Exception as exc:
        # Reading or writing the results failed, not the job. The name stays so
        # a resume can retry the collection instead of re-billing the cohort.
        message = str(exc) or "could not apply batch results"
        print(f"cohort batch {job_name} apply failed ({message}); leaving it to resume")
        _mark_prepared_still_running(pool, cohort, prepared)
        return False


def _mark_prepared_still_running(pool: Path, cohort: dict, prepared: list) -> None:
    """Return the batch's sessions to `running` so a resume picks them up.

    Row at a time rather than one `write_cohort`: apply_cohort_batch may
    already have marked some of these done before it failed, and pushing the
    whole in-memory dict back would overwrite those completions with this
    copy's stale view of them.
    """
    for entry, _ in prepared:
        if entry.get("status") == "done":
            continue
        mark_cohort_session(pool, cohort, entry, "running")


async def apply_cohort_batch(
    pool: Path,
    cohort: dict,
    prepared: list,
    *,
    job_name: str | None = None,
) -> None:
    """Wait for a batch job and write per-session analysis.json results."""
    name = job_name or (cohort.get("batch") or {}).get("name")
    if not name:
        raise RuntimeError("cohort has no batch job to apply")

    by_id = {entry["session_id"]: (entry, digest) for entry, digest in prepared}
    job = await gemini.wait_gemini_batch(name)
    state = gemini._batch_state_name(job)
    if state in ("JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"):
        err = getattr(job, "error", None)
        message = getattr(err, "message", None) if err else None
        message = message or f"Gemini batch {state}"
        for entry, _ in prepared:
            write_job(pool / entry["session_id"], "error", error=message)
            mark_cohort_session(pool, cohort, entry, "error", error=message)
        return

    results = gemini.parse_batch_results(job)
    for sid, (entry, digest) in by_id.items():
        session_dir = pool / sid
        hit = results.get(sid)
        if not hit:
            message = "missing from batch results"
            write_job(session_dir, "error", error=message)
            mark_cohort_session(pool, cohort, entry, "error", error=message)
        elif hit.get("error"):
            write_job(session_dir, "error", error=hit["error"])
            mark_cohort_session(pool, cohort, entry, "error", error=hit["error"])
        else:
            write_analysis_result(
                session_dir,
                hit["text"],
                frame_count=len(digest.get("frames") or []),
                warnings=digest.get("warnings") or [],
            )
            mark_cohort_session(pool, cohort, entry, "done", analysis=ANALYSIS_FILE)


async def _cohort_sync_model(pool: Path, cohort: dict, prepared: list) -> None:
    """Interactive Gemini fallback used when Batch is off or unavailable."""

    async def one(entry: dict, digest: dict) -> None:
        session_dir = pool / entry["session_id"]
        write_job(session_dir, "running")
        mark_cohort_session(pool, cohort, entry, "running")
        try:
            _, model_gate = runtime._gates()
            async with model_gate:
                text = await gemini.call_gemini(
                    digest, digest.get("frames") or [], gemini.session_metadata(session_dir)
                )
            write_analysis_result(
                session_dir,
                text,
                frame_count=len(digest.get("frames") or []),
                warnings=digest.get("warnings") or [],
            )
            mark_cohort_session(pool, cohort, entry, "done", analysis=ANALYSIS_FILE)
        except Exception as exc:
            message = str(exc) or "analysis failed"
            write_job(session_dir, "error", error=message)
            mark_cohort_session(pool, cohort, entry, "error", error=message)

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
    projects = data_root / PROJECTS_DIR
    if projects.is_dir():
        for path in projects.iterdir():
            sessions = path / "sessions"
            if sessions.is_dir():
                pools.append(sessions)
    return pools


def cohorts_needing_batch_resume(data_root: Path) -> list:
    """(pool, cohort) pairs with an in-flight Batch job name.

    An index lookup over every pool at once. The directory scan this replaced
    parsed every cohort file in every pool on the server's startup path.
    """
    pools = {pool_key(pool): pool for pool in iter_session_pools(data_root)}
    # Touch each pool so a not-yet-migrated cohort is resumable on first boot.
    for pool in pools.values():
        cohort_store(pool)
    conn = analyze_store.connect(data_root)
    out = []
    for key, cohort in analyze_store.resumable_cohorts(conn):
        pool = pools.get(key)
        if pool is not None:
            out.append((pool, cohort))
    return out


# --- The patching contract -------------------------------------------------
#
# Tests still swap some expensive edges by assignment on this package
# (`analyze.run_extractor = fake`, `analyze.CUT_PROMPT = "..."`) and drive the
# real pipeline. That only works while the call site resolves the name at call
# time through a namespace the assignment reaches — so those remaining symbols
# must not be from-imported into a caller.
#
# Gemini sync/batch I/O has moved to `gemini.py`. Orchestration here calls
# through the module object (`gemini.call_gemini(...)`, etc.), and tests patch
# at the definition site with `unittest.mock.patch("capture.analyze.gemini…")`.
# Package re-exports of those names are for `analysis_port` and other readers,
# not for assignment patching.
#
# Config and `_gates` live in `runtime.py`. Rebound test hooks
# (`ANALYSIS_ENABLED`, `ANALYZE_BATCH`, `_gate_loop`, the concurrency caps)
# must be assigned on `analyze.runtime`, not on the package — a package
# assignment would not reach `_gates` or the orchestration call sites above.
# `_migrated` and `_undeclared_transitions` are only **mutated in place**, so a
# re-export binds the same object and `.clear()` still reaches the definition.
#
# See docs/analyze-design-review.md §4.1 and the modularize plan.

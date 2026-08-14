# 04 — Cohorts, cuts and the caching model

A cohort is a frozen sample of sessions. A cut is a filter over that cohort, analysed
once and cached. This document covers why the freezing matters, what the cache key
must contain, and why the numbers in the prompt are computed in Python.

Line citations into `__init__.py` below are historical — those functions now live
in `stats.py`, `cuts.py`, `cohorts.py`, and `cohort_state.py`.

---

## The constraint: the model invents numbers

Asked to count across dozens of write-ups, the model produces plausible shares that
are wrong. This is not a prompting problem — it was attacked with prompting first and
the failure persisted. The record is in `stats.py`:

> ```
> Asked to count across write-ups, the model invents shares. So every number that
> reaches the cut prompt is computed here in Python and handed over as given --
> `cross_tab_block` renders the block the prompt reads, and the prompt's job is
> to interpret it, not to derive it.
> ```
> — [stats.py:3](../../capture/analyze/stats.py#L3)

This is the doctrine the whole cut path is built around: **Python computes, the model
interprets.** Every deviation from it has been a bug.

### The three gates each block a different failure

[stats.py:52](../../capture/analyze/stats.py#L52):

| Gate | Value | Blocks |
| --- | --- | --- |
| `CROSS_TAB_MIN_CELL` | 3 | "a share of 2 sessions is noise, not a finding" |
| `CROSS_TAB_MIN_COVERAGE` | 0.20 | "a dimension known for a handful of rows describes the handful, not the cohort" |
| `CROSS_TAB_MIN_LIFT_PTS` | 15.0 | "a difference smaller than this is not worth an action, and the model will happily write one anyway" |

> ```
> A cell must clear all three gates. Count alone lets 3-of-200 through;
> coverage alone lets a large but unremarkable cell through; lift alone lets a
> two-session fluke through.
> ```

**`graded` is the subtle one.** In a single-outcome cut — every row purchased — lift
is 0 *by construction*, so grading on lift suppresses every real split and the cut
reads as though nothing differed. Those cuts grade on composition instead
([stats.py:19](../../capture/analyze/stats.py#L19)).

### "Examined and not reported"

Splits that fail a gate are still **named**:

> ```
> Splits that fail a gate are still named, under "Examined and not reported" --
> without it the model reads silence as uniformity and asserts the opposite.
> ```

Omitting a weak split is not neutral. Absence reads as evidence of sameness, and the
model will state it. Deleting this block to shorten the prompt inverts conclusions.

---

## Guarding the output too

Prompting alone did not stop the model attaching a cohort-wide total to a small
group, so there is a code-level check.
[`_share_denominator_ok`](../../capture/analyze/__init__.py#L1042) parses `"N of M"`
out of a share and drops it when `M` is not the group size:

> ```python
> The model has been seen to write the cohort-wide total into a share for a
> much smaller filtered group. A share whose denominator is not `n` is not
> describing this group, so it is dropped rather than shown as fact.
> ```

Note the design choice: the **observation is kept, the share is dropped**
([:1080](../../capture/analyze/__init__.py#L1080)). A missing share renders as
nothing; a wrong one renders as fact.

[`_given_facts`](../../capture/analyze/__init__.py#L874) attacks the same failure from
the input side, stating the group size as the only usable total:

> ```python
> f"- This group contains exactly {n} session(s). {n} is the ONLY total "
> f"you may use. Every share you write is out of {n} — never out of any "
> f"larger number, and never out of the whole cohort."
> ```

It also tells the model what the filter already guarantees, so a constant is not
reported as a discovery ("All 9 sessions purchased" is not a finding in a
purchased-only cut), and redirects clustering to the dimensions that still vary
([:916](../../capture/analyze/__init__.py#L916)).

---

## Why a cohort is frozen

> ```
> A cohort is immutable
> once sampled, so a "cut" across it can be analysed once and cached beside it.
> ```
> — [__init__.py:12](../../capture/analyze/__init__.py#L12)

Immutability is what makes the cut cache sound. If membership could change, every
cached cut would silently describe a different population than the one on screen, and
there would be no event to invalidate on. Freezing membership turns "is this cut
stale?" into a question about the *question*, not the data —
which is exactly what `cut_key` encodes.

`build_cohort` therefore records not just the members but the **rule** that produced
them (`a_n`, `b_n`, `slice`, `order`, `min_duration_ms`, `url_contains`,
time bounds) plus a human-readable `selection` string
([:1839](../../capture/analyze/__init__.py#L1839)).

### `persistable_cohort` and the `selection` trap

`enrich_cohort_status` backfills `selection` when absent. That backfill is a *guess*
from whatever sampling fields are present — and for a cohort with no counts stored,
the guess reads *"latest 0 for A"*.

> ```python
> Since the backfill only fires when the field is missing, writing that guess makes
> it permanent — in the one field that records how the cohort was built.
> ```
> — [__init__.py:708](../../capture/analyze/__init__.py#L708)

Hence the `stored` parameter: it distinguishes "`selection` was genuinely recorded"
from "this pass invented it." Drop the parameter and the corruption is
write-once-then-permanent.

The same function strips `progress`, because a persisted tally is a second copy of
`sessions` that any other writer updates out from under
([:681](../../capture/analyze/__init__.py#L681)).

---

## What `cut_key` hashes

> ```python
> A cut's answer is a function of which sessions were selected *and* what the
> model was asked. Keying on the filter alone meant a reworded CUT_PROMPT
> kept serving answers generated by the old one, with no invalidation path
> and no signal — the response still said `cached: true`. Since prompt work
> is most of this module's history, that was the common case, not an edge.
> ```
> — [__init__.py:809](../../capture/analyze/__init__.py#L809)

So the key is `{filter: normalize_slice(spec), prompt: prompt_version(CUT_PROMPT)}`
([:824](../../capture/analyze/__init__.py#L824)).

Two deliberate omissions:

- **The model is not in the key.** Switching `GEMINI_MODEL` is rare and intentional,
  and every cached cut records the `model` that produced it. Including it would
  invalidate every cohort's cache on an env-var change.
- **The cohort id is not in the key** — it does not need to be. Cuts are stored per
  cohort (`cohort_cuts` rows keyed by `(pool, cohort_id, key)`), so the scope is
  structural.

### The prompt hash is real money

`prompt_version` is a content hash ([:796](../../capture/analyze/__init__.py#L796)) —
deliberately not a hand-maintained constant, because *"a version number someone has
to remember to bump is exactly the mechanism that rots."*

The consequence is stated in `prompts/__init__.py`:

> ```
> No `.strip()`, no normalisation: trailing whitespace is part of the content
> hash, and `session.md` genuinely ends with a trailing space that was in the
> literal. Stripping it here would silently invalidate every cached cut.
> ```

Current hashes: `session.md` → `5637eecc`, `cut.md` → `d6416d2b`, `chat.md` →
`6007448b`. **Reformatting a prompt file re-bills every cut in the system.** An
editor that trims trailing whitespace on save is sufficient to do it.

---

## Citation numbering is data, not derivation

[`_cut_side_text`](../../capture/analyze/__init__.py#L843) numbers the write-ups for
the model *and returns the mapping*:

> ```python
> The `citations` map returned alongside is that same numbering as data —
> {"1": session_id, ...} — built in this loop, so the numbers the model was
> shown and the ids the UI resolves cannot drift apart. Callers must store
> what they get back rather than rebuilding it from the row list.
> ```

`build_cut_analysis` stores it verbatim
([:1192](../../capture/analyze/__init__.py#L1192)). Rebuilding `{"1": rows[0].id}`
later would look equivalent and would silently repoint every deep link the first time
row ordering changed.

Every clusterable field is stated on **every** session, `"unknown"` included
([:859](../../capture/analyze/__init__.py#L859)) — *"a missing label reads as a
difference the model would otherwise cluster on by accident."*

---

## Chat reuses the cut's context deliberately

[`chat_context`](../../capture/analyze/__init__.py#L1294) mirrors
`build_cut_analysis`'s setup and returns `spec` so the caller can hand it to
`stream_gemini_chat`:

> ```python
> `spec` comes back so the caller can hand it to
> stream_gemini_chat: without it the chat would rebuild its context without
> the measured splits and answer from a thinner view than the cut on screen.
> ```

The evidence goes in as the first user turn followed by a canned model
acknowledgement ([:1236](../../capture/analyze/__init__.py#L1236)) — which keeps the
write-up block a **stable prefix** across turns, and that is what makes implicit
caching possible. Reordering the turns silently doubles the cost of every follow-up
question.

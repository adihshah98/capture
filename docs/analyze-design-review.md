# analyze — design review

*Reviewed 2026-08-11 against `main` @ `d2f35e8`. Part 1 of 3:
**design** · [bugs](analyze-bug-review.md) · [cleanups](analyze-cleanups.md).*

This document is about **structure**: how the package is decomposed, whether the
proposed split is the right one, and what the code's shape (file sizes, function
lengths, parameter counts, packaging) says about where it is under strain. Defects
live in the [bug review](analyze-bug-review.md); mechanical tidying lives in
[cleanups](analyze-cleanups.md).

For how the system actually works, read [docs/deep-dive/](deep-dive/README.md) first.

---

## 1. Where the package stands

`capture/analyze/` is a package with four submodules and a 3,026-line `__init__.py`
holding all orchestration.

| File | Lines | Functions | Role |
| --- | --- | --- | --- |
| `__init__.py` | **3,026** | 121 | config, storage facade, state machine, prompts, sampling, model I/O, orchestration, CLI |
| `store.py` | 675 | 36 | persistence (Postgres) |
| `slices.py` | 292 | 10 | the slice language — pure |
| `stats.py` | 273 | 6 | measured splits — pure |
| `prompts/` | 37 | 1 | prompt loading |

Two prior extractions (`slices.py`, `stats.py`) were done well, and the reason is
worth naming because it is the rule the rest of the work should follow: **both are
pure leaves.** No I/O, no module state, no back-edge into the parent. The dependency
graph is a clean DAG — `__init__ → {slices, stats, prompts, store}`, `stats → slices`,
`store → capture.{db,projects}` — and no submodule imports its parent.

That DAG is an asset. Every proposal below is constrained by preserving it.

---

## 2. Code shape: where the strain shows

Structure arguments are easy to hand-wave, so here are measurements.

### 2.1 Function length

Only 8 functions in `__init__.py` exceed 60 lines, which is better than a
3,000-line file usually implies. The long ones are long for different reasons:

| Function | Lines | Params | Verdict |
| --- | --- | --- | --- |
| `run_cohort_job` | 116 | 3 | **Acceptable.** It is a state machine with a `try/except CancelledError` wrapper. Splitting it would scatter the batch-billing invariant across functions that must not be reordered. |
| `sample_cohort_rows` | 102 | **14** | **Too many parameters** — see 2.2. |
| `build_cohort` | 88 | **15** | Same. |
| `enrich_cohort_status` | 85 | 2 | Acceptable — one loop with a documented branch per case. |
| `parse_batch_results` | 70 | 1 | Acceptable — two SDK response shapes, both required. |
| `_given_facts` | 63 | 2 | Acceptable — prompt text assembly. |
| `_cli_run_cohort` / `main` | 62 / 61 | 8 / 1 | **Delete** (see bug review §1). |

The honest read: **function length is not this file's problem.** Parameter count is.

### 2.2 Parameter threading is the real smell

Seven functions take 8+ parameters, and they form one chain:

```
build_cohort(15) → sample_cohort_rows(14) → _sample_matching_rows(11)
                                          → _sample_candidates(8)
                                          → cohort_selection_text(12)
                                          → _auto_cohort_label(9)
```

The same ~12 values — `purchased_n, all_n, any_n, a_n, b_n, min_duration_ms,
url_contains, slice_spec, slice_b, start_ms, end_ms, order` — are re-typed at every
layer. `sample_cohort_rows` already tries to mitigate it with a `sample_kw` dict
([:1558](../capture/analyze/__init__.py#L1558)), which is the code telling you what it
wants.

**This is the clearest refactor in the package:** a `CohortSpec` dataclass holding the
sampling vocabulary. `build_cohort` builds one, everything downstream takes it, and
the `fields` JSON column in `store.py` already persists exactly this shape. It would
remove ~50 parameter declarations and make "add a sampling dimension" a one-line
change instead of a seven-signature one.

It also fixes the worst offender **outside** the package:
[`create_cohort`](../capture/server.py#L1339) takes **29 parameters** across 135
lines — every sampling knob re-declared a third time as FastAPI `Query()` objects.

### 2.3 File size

`__init__.py` at 3,026 lines is too big, but the reason matters. It is not one
concern that grew — it is **eight concerns that never separated**, interleaved in
source order (see [deep-dive README](deep-dive/README.md)). A reader looking for
sampling finds it between prompt assembly and cohort labels.

`server.py` at 2,242 lines has the same shape and is out of scope here, but
`create_cohort` (135 lines / 29 params) is the single worst function in the repo.

### 2.4 Packaging

`pyproject.toml` is tool configuration only — dependencies stay in
`requirements.txt`, and there is no `[project]` table, so `capture` is not an
installable package. It works because everything runs from the repo root
(`uvicorn capture.server:app`). Fine for now; worth revisiting only if the CLI is
ever reinstated or the package is consumed elsewhere.

**The finding that matters:** `capture/analyze` is *excluded from both linters*.

```toml
# capture/analyze is the co-founder's; src/analyze is its Node extractor.
extend-exclude = ["capture/analyze", "src/analyze", ...]

[[tool.mypy.overrides]]
module = "capture.analyze.*"
ignore_errors = true
```

CI enforces `ruff` + `mypy` on `capture/` — and skips the largest, most complex file
in it. That exclusion is why `conn: sqlite3.Connection` can be false on every live
call path with nothing complaining, and why unused imports and dead functions persist.

The ownership rationale is now stale: this review exists *because* the file is being
worked on. **Lifting the exclusion should be a deliverable of this refactor**, done
last so the diff is not buried in reformatting. Expect `--fix`-able noise plus real
type errors around the compat connection.

*(Local note: `ruff` and `mypy` are not installed in `.venv`, so `npm run lint` fails
locally while passing in CI, which installs `requirements-dev.txt`.)*

---

## 3. The module split: the proposed seam is wrong

The [existing plan](analyze-refactor.md) proposes `extract.py` → `models.py` →
`pipeline.py` → `cli.py`. **This is the wrong decomposition** and should not be done.

**It cuts by technical layer, but the dependencies do not run that way.**
`run_cohort_job` alone touches storage, state, sampling, prompts and model I/O.
Splitting by layer produces four modules that all import each other.

**Every cross-module call becomes indirect.** The forwarding rule (deep-dive
[07](deep-dive/07-patch-forwarding.md)) forbids `from .models import wait_gemini_batch`
— it must be `models.wait_gemini_batch(...)`. So the result is four files with
circular module-object references and a rule that must be obeyed perfectly at every
site, with **no test that catches a violation**. The failure mode is a green suite
making billed Gemini calls.

**It breaks on `_gates`.** `_gates()` has five call sites across what would become
three modules — plus [`server.py:1788`](../capture/server.py#L1788), outside the
package entirely. The plan's own mitigation ("keep `_gates` in `__init__.py`") is an
admission the seam does not hold.

## 4. The recommended decomposition

Change the rule from *by layer* to **by dependency depth**: extract only what has
strictly fewer dependencies than what remains. Everything extracted is a leaf. The
orchestrator stays whole and stays the only module that knows about all of it.

```
capture/analyze/
  slices.py       ✅ done   pure predicates over dicts          (no deps)
  stats.py        ✅ done   measured splits                     (slices)
  prompts/        ✅ done   prompt text + load()                (no deps)
  attribution.py     NEW    the single URL→attribution parser   (no deps)
  cutfmt.py          NEW    cut response normalisation          (no deps)
  labels.py          NEW    selection/label text                (slices)
  spec.py            NEW    CohortSpec dataclass                (slices)
  sampling.py        NEW    SQL + candidate filtering           (slices, spec, db)
  store.py        ✅ done   persistence
  __init__.py               config, gates, storage facade, state machine,
                            model I/O, orchestration, forwarding
```

| Module | Contents | Why it is safe |
| --- | --- | --- |
| **`attribution.py`** | `ATTRIBUTION_PARAMS`, one parser | ~25 pure lines imported by both `analyze` and `server`. Fixes the twins structurally. **Do first** — cheapest, retires a bug class. |
| **`cutfmt.py`** | `empty_cut_analysis`, `_clean_text`, `_insight_item`, `_recommendation_line`, `_recommendations_from_actions`, `_share_denominator_ok`, `normalize_cut_analysis` (963–1100) | Pure functions over model JSON. ~140 lines, zero risk. |
| **`labels.py`** | `_slice_scope_text`, `_format_cohort_clock`, `_time_window_text`, `cohort_selection_text`, `_slice_label_bits`, `_auto_cohort_label`, `clean_cohort_label` (1634–1771) | Pure string generation. ~140 lines. |
| **`spec.py`** | `CohortSpec` + the `COHORT_*` constants | Collapses the 15-parameter chain (§2.2). |
| **`sampling.py`** | `pool_facets`, `_cohort_where`, `_cohort_order_sql`, `_sample_*`, `sample_cohort_rows`, `index_row_facts`, `facets_from_rows`, `_facet_counts_listed` (1311–1631) | Takes a connection, otherwise self-contained. **Exactly the set the Postgres port must touch** — extracting first means that work happens in a 250-line file. |

Net: ~570 lines out, `__init__.py` to ~2,450 — still large, but every remaining line
is orchestration or the config it needs. That is a coherent module rather than a
leftover pile.

**Deliberately not extracted:** model I/O and orchestration. They share `_gates`,
config and the storage facade, and they are the exact surface the tests patch.
Splitting them buys tidier file sizes and pays in forwarding fragility across a paid
API path. Leave them.

### Preconditions

1. **Test the forwarding mechanism first** (deep-dive 07). Nothing currently asserts
   it works; if `_FORWARD_TO` were emptied, every test would still pass.
2. **Promote `_gates` to a public accessor** and update `server.py:1788`. This is the
   only external use of the package's private surface.
3. **Add each new module to `_FORWARD_TO`** as it is created.

---

## 5. Design decisions worth preserving

Load-bearing choices that look incidental. The deep dives cover the reasoning; this
is the list not to "simplify."

- **`_ForwardingModule`** — the right instinct, even though it currently protects
  almost nothing ([07](deep-dive/07-patch-forwarding.md)).
- **Owners discovered via `vars()`**, not a hand-maintained map. The comment predicts
  exactly the failure the attribution twins then demonstrated.
- **`slices.py` as pure predicates** — what makes the sampler testable without a pool
  and translatable to SQL later.
- **Python computes, the model interprets** — measured splits, the three gates,
  `graded`, and "Examined and not reported" ([04](deep-dive/04-cohorts-cuts-caching.md)).
- **Citation numbering returned as data**, never rebuilt from row order.
- **`_share_denominator_ok`** — a code guard where prompting demonstrably failed.
- **Streaming batch submission** with its load-bearing `del`s
  ([03](deep-dive/03-model-paths-batch-and-sync.md)).
- **Only submission may fall back** — the most expensive invariant to break.
- **Status as projection, not record** — terminal states recomputed
  ([06](deep-dive/06-state-concurrency-failure.md)).
- **`persistable_cohort`'s `stored` parameter** — prevents a write-once-permanent
  corruption of `selection`.
- **Two gates for two resources**, created lazily against the running loop.

---

## 6. Recommended sequence

Structural work only; see the other two documents for their own ordering.

1. Forwarding mechanism test *(precondition)*.
2. `attribution.py` — retires the twins.
3. `cutfmt.py`, then `labels.py` — pure, mechanical.
4. `spec.py` + thread `CohortSpec` through the sampling chain; simplify
   `create_cohort` in `server.py`.
5. `sampling.py` — then the Postgres port inside it.
6. Public `model_gate()` accessor; update `server.py:1788`.
7. **Lift the ruff/mypy exclusion** and fix what it surfaces.

One commit each, full `npm test` between. Do **not** do the
`extract/models/pipeline/cli` split.

### Verification

- `npm test` (438 tests) green after every commit.
- Prompt hashes unchanged: `5637eecc` / `d6416d2b` / `6007448b`. Never edit the
  `.md` prompt files — `cut_key` bakes the hash into cached keys and a whitespace
  change re-bills every cut.
- After step 1, deliberately breaking forwarding must turn the suite **red**.
- After any layout change, boot `uvicorn capture.server:app` and check `/healthz` for
  `bundles_present` and `analysis_runtime_present` — nothing in the suite catches a
  wrong path anchor.

# analyze — design review

*Reviewed 2026-08-11, re-verified against* `main` *@* `70f5748`*. Part 1 of 3:
**design** · [bugs](analyze-bug-review.md) · [cleanups](analyze-cleanups.md).*

> **Re-verification note.** Fifteen commits landed after the first draft. They left
> `capture/analyze/` **byte-identical** — every measurement in §1–§2 still holds — but
> restructured everything around it. Three consequences, detailed in §0:
> `capture/analysis_port.py` now exists and resolves two of this document's
> preconditions; `server.py` is down to 1,731 lines; and the arrival of the port
> changes the recommendation on `_ForwardingModule` from "keep" to "delete."

This document is about **structure**: how the package is decomposed, whether the
proposed split is the right one, and what the code's shape (file sizes, function
lengths, parameter counts, packaging) says about where it is under strain. Defects
live in the [bug review](analyze-bug-review.md); mechanical tidying lives in
[cleanups](analyze-cleanups.md).

For how the system actually works, read [docs/deep-dive/](deep-dive/README.md) first.

---



## 0. What changed since the first draft

Verified with `git diff d2f35e8..70f5748`. `capture/analyze/` **has a zero-line
diff** — the package this document reviews is unchanged. What moved is its
surroundings, and two of those moves land squarely on this document's recommendations.

### 0.1 `capture/analysis_port.py` — a declared interface (commit `bbcc931`)

The web layer no longer imports `analyze`. `server.py` imports
`analysis_port as ap`, and [test_analysis_port.py:153](../tests/test_analysis_port.py#L153)
enforces that nothing else in `capture/` may reach the package directly. The port's
own docstring records what it replaced:

> ```
> Before this module, server.py reached into 51 of its names — including
> `analyze._gates()`, a private — so any rename over there was a breaking change
> to the web layer, and neither side could see the interface it was depending on.
> ```

It also collapses the three identity notions (`pool: Path`, `pool_key: str`,
`data_root: Path`) into one `Project` argument — which is the seam that will matter
when the package stops being addressed by filesystem path.

**This resolves precondition #2 of §4.** `_gates()` is no longer reached privately
from outside: [analysis_port.py:63](../capture/analysis_port.py#L63) exposes
`model_gate()`, and [server.py:1526](../capture/server.py#L1526) uses it. All five
remaining `_gates()` call sites are now *inside* the package.

### 0.2 Tooling and structure already landed


| Item                            | Where it was | Status                                                               |
| ------------------------------- | ------------ | -------------------------------------------------------------------- |
| `SAFE_CHUNK` deduplicated       | cleanups #3  | **Done** — now `capture/paths.py`; only `analyze`'s own copy remains |
| `ruff format` + mypy gate in CI | cleanups #9  | **Done** for `capture/`; `analyze` still excluded                    |
| Fast/DB test tiers              | —            | **New** — `pytest -m unit`, enforced by `conftest.py`                |
| `server.py` size                | §2.3         | 2,242 → **1,731** lines (routes, auth, settings, CLI extracted)      |
| Test count                      | verification | 438 → **506**                                                        |




### 0.3 The recommendation that changed: drop `_ForwardingModule`

§4 originally kept the forwarding mechanism and added a test for it. **That is no
longer the right call**, for a reason the port supplies.

Forwarding exists to keep `analyze.X = fake` reaching a submodule's globals. Its
justification was that `server.py` and the tests reach for names off `analyze`
directly. The port removed the production half of that: production now reaches
`ap.*`, and the port already states the correct rule as a convention —

> ```
> Call through `analyze.<name>` inside the function body rather than binding
> the attribute at import.
> ```

That is the same invariant the metaclass enforces, achieved by convention in one
278-line file. Re-measured against `HEAD`, the re-export blocks the mechanism serves
have almost no consumers left:


| Re-exported symbol                                                        | Production            | Tests |
| ------------------------------------------------------------------------- | --------------------- | ----- |
| `META_SOURCES`, `slice_is_empty`, `is_meta_attribution`, `OUTCOME_LABELS` | 0                     | 0     |
| `cross_tab_cells`, `CROSS_TAB_*` (5 constants)                            | 0                     | 0     |
| `MOBILE_MAX_WIDTH`, `slice_label`                                         | 0                     | 1     |
| `device_class`, `page_path`, `outcome_class`                              | 1 each (via the port) | 6–14  |
| `normalize_slice`, `cross_tab_block`, `row_matches_slice`                 | 0                     | 4–18  |


Ten re-exported names have **zero** consumers anywhere. The rest are reached almost
entirely by tests, which can import `capture.analyze.slices` directly.

Against that, the mechanism's costs are concrete: it is invisible to type checkers
and IDEs; it fires on every module-global write including internal `global`
statements; `OUTCOME_LABELS` is in `vars()` of both `slices` and `stats` and the
loop has no `break`, so one assignment writes three bindings; and the class swap is
the last statement of a 3,026-line module, so any import-time exception disables it
silently. It is also propping up a testing style — **54 raw assignments versus one**
`unittest.mock.patch` — rather than being required by one.

**Revised recommendation:** narrow the re-exports to what is consumed, then delete
`_ForwardingModule` and `_FORWARD_TO`. Keep the "call through the module object"
rule as a convention, enforced by an AST test in the style of
`test_analysis_port.py`. This removes the need for the forwarding test that was
precondition #1, and replaces a metaclass with an ordinary Python package.

---



## 1. Where the package stands

`capture/analyze/` is a package with four submodules and a 3,026-line `__init__.py`
holding all orchestration.


| File          | Lines     | Functions | Role                                                                                    |
| ------------- | --------- | --------- | --------------------------------------------------------------------------------------- |
| `__init__.py` | **3,026** | 121       | config, storage facade, state machine, prompts, sampling, model I/O, orchestration, CLI |
| `store.py`    | 675       | 36        | persistence (Postgres)                                                                  |
| `slices.py`   | 292       | 10        | the slice language — pure                                                               |
| `stats.py`    | 273       | 6         | measured splits — pure                                                                  |
| `prompts/`    | 37        | 1         | prompt loading                                                                          |


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


| Function                   | Lines   | Params | Verdict                                                                                                                                                                               |
| -------------------------- | ------- | ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `run_cohort_job`           | 116     | 3      | **Acceptable.** It is a state machine with a `try/except CancelledError` wrapper. Splitting it would scatter the batch-billing invariant across functions that must not be reordered. |
| `sample_cohort_rows`       | 102     | **14** | **Too many parameters** — see 2.2.                                                                                                                                                    |
| `build_cohort`             | 88      | **15** | Same.                                                                                                                                                                                 |
| `enrich_cohort_status`     | 85      | 2      | Acceptable — one loop with a documented branch per case.                                                                                                                              |
| `parse_batch_results`      | 70      | 1      | Acceptable — two SDK response shapes, both required.                                                                                                                                  |
| `_given_facts`             | 63      | 2      | Acceptable — prompt text assembly.                                                                                                                                                    |
| `_cli_run_cohort` / `main` | 62 / 61 | 8 / 1  | **Delete** (see bug review §1).                                                                                                                                                       |


The honest read: **function length is not this file's problem.** Parameter count is.

### 2.2 Parameter threading is the real smell

Seven functions take 8+ parameters, and they form one chain:

```
build_cohort(15) → sample_cohort_rows(14) → _sample_matching_rows(11)
                                          → _sample_candidates(8)
                                          → cohort_selection_text(12)
                                          → _auto_cohort_label(9)
```

The same ~12 values — `purchased_n, all_n, any_n, a_n, b_n, min_duration_ms, url_contains, slice_spec, slice_b, start_ms, end_ms, order` — are re-typed at every
layer. `sample_cohort_rows` already tries to mitigate it with a `sample_kw` dict
([:1558](../capture/analyze/__init__.py#L1558)), which is the code telling you what it
wants.

**This is the clearest refactor in the package:** a `CohortSpec` dataclass holding the
sampling vocabulary. `build_cohort` builds one, everything downstream takes it, and
the `fields` JSON column in `store.py` already persists exactly this shape. It would
remove ~50 parameter declarations and make "add a sampling dimension" a one-line
change instead of a seven-signature one.

It also fixes the worst offender **outside** the package:
`[create_cohort](../capture/server.py#L1339)` takes **29 parameters** across 135
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

Since the first draft the rest of `capture/` has been formatted and type-gated
(`067fe83`, `d177c4c`, `fb82347`), and `requirements-dev.txt` now exists — so
`capture/analyze` is the **only** remaining exclusion, and the argument for lifting it
is stronger than when this was written.

### 2.5 `server.py` is no longer the counter-example

The first draft called `server.py` (2,242 lines) the same shape as
`analyze/__init__.py` and put it out of scope. It has since been reduced to 1,731
lines by extracting routes, auth, settings and the CLI — using the same
peel-off-leaves rule §4 recommends here. That is a useful precedent: the approach
below is not hypothetical in this codebase.

`create_cohort` (135 lines / **29 parameters**) survives untouched and remains the
worst function in the repo. §2.2's `CohortSpec` is its fix.

---



## 3. The module split: the proposed seam is wrong

The superseded plan (`docs/analyze-refactor.md`, deleted in `1a913be`) proposed
`extract.py` → `models.py` →
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

**It breaks on** `_gates`**.** `_gates()` has five call sites that would land in three
different modules ([:1189](../capture/analyze/__init__.py#L1189),
[:2486](../capture/analyze/__init__.py#L2486),
[:2519](../capture/analyze/__init__.py#L2519),
[:2795](../capture/analyze/__init__.py#L2795), plus
[analysis_port.py:66](../capture/analysis_port.py#L66)). They must return **one
shared pair** of semaphores — `tests/test_analyze_concurrency.py` pins that
extraction is memory-bound (1 Chromium) and model calls network-bound (3). Split them
across modules and each gets its own limit. The plan's own mitigation ("keep `_gates`
in `__init__.py`") is an admission the seam does not hold.

*Since the first draft the sixth, external call site has been fixed: the port's*
`model_gate()` *now owns that access, so the constraint is entirely internal.*

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


| Module           | Contents                                                                                                                                                                    | Why it is safe                                                                                                                                                |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `attribution.py` | `ATTRIBUTION_PARAMS`, one parser                                                                                                                                            | ~25 pure lines imported by both `analyze` and `server`. Fixes the twins structurally. **Do first** — cheapest, retires a bug class.                           |
| `cutfmt.py`      | `empty_cut_analysis`, `_clean_text`, `_insight_item`, `_recommendation_line`, `_recommendations_from_actions`, `_share_denominator_ok`, `normalize_cut_analysis` (963–1100) | Pure functions over model JSON. ~140 lines, zero risk.                                                                                                        |
| `labels.py`      | `_slice_scope_text`, `_format_cohort_clock`, `_time_window_text`, `cohort_selection_text`, `_slice_label_bits`, `_auto_cohort_label`, `clean_cohort_label` (1634–1771)      | Pure string generation. ~140 lines.                                                                                                                           |
| `spec.py`        | `CohortSpec` + the `COHORT_*` constants                                                                                                                                     | Collapses the 15-parameter chain (§2.2).                                                                                                                      |
| `sampling.py`    | `pool_facets`, `_cohort_where`, `_cohort_order_sql`, `_sample_*`, `sample_cohort_rows`, `index_row_facts`, `facets_from_rows`, `_facet_counts_listed` (1311–1631)           | Takes a connection, otherwise self-contained. **Exactly the set the Postgres port must touch** — extracting first means that work happens in a 250-line file. |


Net: ~570 lines out, `__init__.py` to ~2,450 — still large, but every remaining line
is orchestration or the config it needs. That is a coherent module rather than a
leftover pile.

**Deliberately not extracted:** model I/O and orchestration. They share `_gates`,
config and the storage facade, and they are the exact surface the tests patch.
Splitting them buys tidier file sizes and pays in forwarding fragility across a paid
API path. Leave them.

### Preconditions

1. ~~Test the forwarding mechanism first.~~ **Superseded by §0.3** — narrow the
  re-exports and delete the mechanism instead. Do this *before* any extraction, so
   the new modules are never wired into a mechanism that is about to go.
2. ~~Promote~~ `_gates` ~~to a public accessor.~~ **Done** —
  [analysis_port.py:63](../capture/analysis_port.py#L63).
3. **Route new production consumers through** `analysis_port.py`, not through fresh
  imports of `capture.analyze`. `test_analysis_port.py` enforces this; a new
   submodule that `server.py` needs is a port function, not a new import.
4. **Keep the call-through-the-module-object rule** for anything tests patch, now as
  a convention plus an AST test rather than a metaclass.

---



## 5. Design decisions worth preserving

Load-bearing choices that look incidental. The deep dives cover the reasoning; this
is the list not to "simplify."

- **The instinct behind** `_ForwardingModule` — that a patched name must reach the
real call site — is correct and must survive the mechanism's deletion (§0.3). What
is preserved is the *rule* (call through the module object, never
`from .x import y` for a patchable name), not the metaclass.
- **Owners discovered via** `vars()`, not a hand-maintained map. The reasoning —
that a hand-maintained list rots the first time a function moves — is right, and
the attribution twins proved it. Any replacement convention must be
machine-checked for the same reason.
- `slices.py` **as pure predicates** — what makes the sampler testable without a pool
and translatable to SQL later.
- **Python computes, the model interprets** — measured splits, the three gates,
`graded`, and "Examined and not reported" ([04](deep-dive/04-cohorts-cuts-caching.md)).
- **Citation numbering returned as data**, never rebuilt from row order.
- `_share_denominator_ok` — a code guard where prompting demonstrably failed.
- **Streaming batch submission** with its load-bearing `del`s
([03](deep-dive/03-model-paths-batch-and-sync.md)).
- **Only submission may fall back** — the most expensive invariant to break.
- **Status as projection, not record** — terminal states recomputed
([06](deep-dive/06-state-concurrency-failure.md)).
- `persistable_cohort`**'s** `stored` **parameter** — prevents a write-once-permanent
corruption of `selection`.
- **Two gates for two resources**, created lazily against the running loop.

---



## 6. Recommended sequence

Structural work only; see the other two documents for their own ordering.

1. **Narrow the re-exports, then delete** `_ForwardingModule` *(precondition, §0.3)*.
  Replace with an AST test asserting submodules never `from .x import y` a
   patchable name.
2. `attribution.py` — retires the twins.
3. `cutfmt.py`, then `labels.py` — pure, mechanical.
4. `spec.py` + thread `CohortSpec` through the sampling chain; then collapse
  `create_cohort`'s 29 parameters. The port already passes them opaquely
   (`build_cohort(conn, project, **kwargs)`,
   [analysis_port.py:157](../capture/analysis_port.py#L157)), so the web layer and
   the package can be changed in one step.
5. `sampling.py` — then the Postgres port inside it.
6. ~~Public~~ `model_gate()` ~~accessor.~~ **Done.**
7. **Lift the ruff/mypy exclusion** on `capture/analyze` and fix what it surfaces.
  The rest of `capture/` is already gated, so this is the last file outside the net.

One commit each, full `npm test` between. Do **not** do the
`extract/models/pipeline/cli` split.

### Verification

- `npm test` green after every commit — **506 tests** at `70f5748` (the earlier
figure of 438 predates the fast/DB tier split).
- `pytest -m unit` for the fast tier; new pure modules should be added to
`UNIT_MODULES` in [tests/conftest.py](../tests/conftest.py) as they land.
- Prompt hashes unchanged: `5637eecc` / `d6416d2b` / `6007448b`. Never edit the
`.md` prompt files — `cut_key` bakes the hash into cached keys and a whitespace
change re-bills every cut.
- After step 1, deliberately breaking forwarding must turn the suite **red**.
- After any layout change, boot `uvicorn capture.server:app` and check `/healthz` for
`bundles_present` and `analysis_runtime_present` — nothing in the suite catches a
wrong path anchor.


# 07 — The patch-forwarding mechanism

> **Historical.** `_ForwardingModule` and package-level re-exports are gone.
> `__init__.py` is a docstring. Tests patch owner modules
> (`capture.analyze.gemini`, `session`, `cuts`, `cohorts`) at the definition site;
> `tests/server/test_patch_contract.py` forbids `from .owner import patched_name`.
> Kept as the record of why forwarding existed and the invariant that must survive it.

---

## The constraint: the tests patch by assignment, and the calls are real

The expensive edges of this pipeline are a headless Chromium and a paid Gemini API.
The tests do not mock them at the boundary; they swap the functions and then drive
**the real pipeline**:

```python
analyze.run_extractor = fake_extract
analyze.submit_prepared_gemini_batch = fake_submit
analyze.wait_gemini_batch = fake_wait
analyze.parse_batch_results = fake_parse
```
— [test_cohort_batch.py:109](../../tests/test_cohort_batch.py#L109)

This is a good design: it exercises `run_cohort_job`'s real control flow, real state
transitions and real persistence. It has one requirement — **the assignment must
reach the call site.**

That works today because caller and callee share one namespace. `run_cohort_job`
calls a bare `submit_prepared_gemini_batch(...)`, which resolves through the module's
globals *at call time*, so rebinding the attribute redirects it.

### The failure mode a split introduces

> ```python
> # Splitting the module breaks that silently. `from .models import call_gemini`
> # copies the value into the submodule's globals at import, so a later
> # `analyze.call_gemini = fake` rebinds only the re-export while the pipeline
> # keeps calling the real one -- a real network call from a test suite that
> # still passes, because the assertions are about cohort state rather than
> # about which function ran.
> ```
> — [__init__.py:2999](../../capture/analyze/__init__.py#L2999)

The severity is specific: not a broken test, but a **green test suite that makes
billed API calls**. The assertions are about cohort state, and a real Gemini response
satisfies them just as well as a fake one.

## The design

```python
_FORWARD_TO = (slices, stats, prompts)

class _ForwardingModule(sys.modules[__name__].__class__):
    def __setattr__(self, name, value):
        for submodule in _FORWARD_TO:
            if name in vars(submodule):
                setattr(submodule, name, value)
        super().__setattr__(name, value)

sys.modules[__name__].__class__ = _ForwardingModule
```
— [__init__.py:3011](../../capture/analyze/__init__.py#L3011)

The module object's class is replaced at import with a `ModuleType` subclass that
intercepts attribute assignment. A write to `analyze.X` is pushed **down** into every
submodule that already has `X`, then set on `analyze` itself.

**Direction: parent → child only.** Setting `slices.device_class = f` does *not*
update `analyze.device_class`.

Ownership is discovered dynamically, and the rationale is the sharpest comment in the
codebase:

> ```python
> # Owners are discovered from `vars()` rather than listed here: a
> # hand-maintained map is the mechanism that rots the first time a function
> # moves between submodules (cf. ATTRIBUTION_PARAMS' "change both together"
> # comment, which is a comment and not a mechanism).
> ```

That prediction is confirmed: the attribution twins **have** diverged.

---

## What it actually protects: almost nothing

This is the part that is not obvious from reading it.

Tracing all 14 patched symbols to their production call sites: **13 are defined
directly in `__init__.py`** and resolve through its own globals. They are patchable
with or without the mechanism.

The 14th is `CUT_PROMPT` — and even there, `__init__.py:840` calls
`prompts.load("cut.md")` itself rather than importing the constant, so both real
consumers (`cut_key`, `call_gemini_cut`) read `__init__`'s own global. Forwarding only
syncs `prompts.CUT_PROMPT`, which **no code reads**.

Verified directly:

```
analyze.CUT_PROMPT is prompts.CUT_PROMPT   -> False   (separate file reads)
analyze.CUT_PROMPT == prompts.CUT_PROMPT   -> True    (equal strings)
analyze.CUT_PROMPT = "FAKE"
prompts.CUT_PROMPT                          -> "FAKE" (forwarded, unread)
```

So `_ForwardingModule` is currently **defensive infrastructure with no live
dependents**. Its value is entirely prospective — it makes the *next* extraction safe
by default. That is a legitimate choice, but it should be a conscious one, because the
cost is a metaclass-level surprise invisible to type checkers, IDEs and `__all__`
tooling.

It does work for a hypothetical future `models.py`:

```
analyze._FORWARD_TO += (fake_models,)
analyze.call_gemini = lambda: "FAKE"
fake_models.call_gemini()  -> "FAKE"
```

---

## What the mechanism does not cover

**`store` is not in `_FORWARD_TO`.** It is imported as
`from . import store as analyze_store` and always called qualified, so it is already
patchable. Correct today — but silent. Adding `from .store import read_cohort` to a
re-export block breaks patching with no error.

**`OUTCOME_LABELS` is multi-owner.** Defined at
[slices.py:245](../../capture/analyze/slices.py#L245), re-imported at
[stats.py:28](../../capture/analyze/stats.py#L28), so it is in `vars()` of *both*
targets. There is no `break`, so one assignment writes three bindings. Benign here —
they should agree — but the mechanism cannot distinguish "one symbol re-exported"
from "two unrelated symbols sharing a name."

**The class swap is the last statement in a 3,026-line module.** Any exception during
import leaves a plain `ModuleType` with no forwarding, and patching silently no-ops.

**Restoration is not transactional.** Tests that save and restore through the parent
([test_cohort_batch.py:205](../../tests/test_cohort_batch.py#L205)) re-forward
correctly. Anything restoring onto a submodule directly would leave `analyze` stale.

**Nothing tests the mechanism.** There is no assertion anywhere that patching
propagates. The protection is invisible: if `_FORWARD_TO` were emptied tomorrow,
every test would still pass — until the first submodule extraction, which would then
make real API calls from a green suite.

---

## The simpler alternative

The two re-export blocks are the *only* reason forwarding is needed:

```python
from .slices import (DEVICE_VALUES, ..., slice_label)      # :779
from .stats import (CROSS_TAB_DIMENSIONS, ..., cross_tab_cells)  # :942
```

They exist because `server.py` and the tests reach for these names off `analyze`.
Replacing them with qualified `slices.foo` / `stats.foo` access at every call site
would give the same guarantee with no metaclass — a strictly simpler mechanism.

That is a larger change (it touches `server.py:1868-1870, 1960, 1963` and several test
files), so it is not the recommendation today. But it is the exit if the forwarding
mechanism ever misbehaves.

---

## The invariant you would break

1. **Never `from .submodule import name` for anything the tests patch.** Import the
   module and call through it: `models.wait_gemini_batch(...)`. This is the whole rule.
2. **Never move the class swap earlier** in the file, or make it conditional. It must
   run after every symbol exists.
3. **Add a submodule to `_FORWARD_TO` when you create it.** Nothing enforces this and
   nothing will fail — until a test's fake is silently ignored and the suite bills a
   real cohort.
4. **Write the mechanism test before relying on it.** Patch a submodule-owned symbol,
   assert the submodule's binding changed. Without it, the protection cannot be
   distinguished from its absence.

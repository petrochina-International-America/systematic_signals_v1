# Knowledge folder

This folder is the **context layer** for the AI panels on the SystematicTrading dashboard.
It is not documentation-for-humans that happens to get fed to a model. It is a
**governed artifact**: the model reads these files, the model paraphrases these files
to Chen, and a stale file here becomes a stale claim on the trading desk.

Treat every file here with the same care as code.

---

## The four layers

The AI panel is deliberately split so that **no layer can do the job of the layer below it.**

```
Layer 4  Panel (React)          renders flags + commentary, visually separated
Layer 3  Prose (LLM)            explains flags in English. Cannot compute.
Layer 2  Context endpoint       assembles the ONLY payload the model may see
Layer 1  Rule engine (Python)   deterministic detection. No model involved.
Layer 0  Knowledge (this folder) methodology the prose layer is allowed to draw on
```

**Layer 1 detects. Layer 3 explains. The model never notices anything on its own.**

This is the central design constraint and every review of this system should check it
first. If a flag can only be produced by the model "noticing" something, it is not a
flag — it is narrative, and it does not ship.

### Layer 0 — Knowledge (this folder)
Markdown files with YAML front-matter. Each file declares which pages it is loaded on
and which flags it explains. The registry loader reads the front-matter; nothing is
hard-coded in Python.

### Layer 1 — Rule engine (`signals/flags.py`)
Pure functions over a signal snapshot. Each returns:

```python
Flag(
    id="sharpe_regime_divergence",
    severity="warn",
    evidence={"sharpe_1y": 2.18, "sharpe_all": 0.67, "ratio": 3.25, "threshold": 2.0},
)
```

The `evidence` dict is the contract. **Every number the model is permitted to utter
must appear in some flag's evidence, or in the snapshot.** See Layer 3.

### Layer 2 — Context endpoint (`GET /api/pages/{page}/context`)
Assembles `{page, as_of, flags, snapshot, docs}`. This payload is the model's entire
universe. There is no query surface, no SQL, no tool the model can reach for. If a
number is not in the payload, it does not exist.

### Layer 3 — Prose (LLM)
Given the payload + the MD files for that page, writes 2–5 sentences explaining what
fired and why it matters. Then the **numeric grounding guard** runs: every numeral in
the model's output is extracted and checked against the payload. Any number that is not
in the payload → the prose is discarded and the panel renders flags only.

This guard is the answer to "how do you know it isn't making things up." It is not a
prompt instruction. It is an assertion.

### Layer 4 — Panel (React)
Two zones, visually distinct:
- **Flags** — deterministic. Hover shows the rule expression and the evidence dict.
- **Commentary** — labelled as model-written. Explains the flags. Never the source of a number.

Provenance is visible at all times. Chen should never have to guess which zone a claim
came from.

---

## File format

Every file in `methodology/`, `flags/`, and `conventions/` carries front-matter:

```yaml
---
id: wti_brent
title: WTI–Brent mean reversion
pages: [signals, levels]
flags: [sharpe_regime_divergence, deprecated_config, low_trade_count]
status: draft          # draft | reviewed | authoritative
owner: mark
last_verified: 2026-07-14
verify_test: tests/knowledge/test_wti_brent_claims.py
---
```

- `pages` — which dashboard pages load this file into the prose context.
- `flags` — which flag ids this file is allowed to explain. The loader uses this to
  select files: a page's prose context = union of files whose `flags` intersect the
  flags that actually fired. **Files whose flags did not fire are not loaded.** This
  keeps the context small and stops the model reaching for unrelated methodology.
- `status` — `draft` files are loaded but the panel shows a "draft methodology" note.
- `verify_test` — path to the integration test that asserts this file's central claim
  still holds against a live backtest run. See below.

## The staleness problem

The single most dangerous failure mode of this system is **a knowledge file drifting
from the code.** The model will confidently teach Chen a convention that stopped being
true three months ago, in fluent prose, next to audited numbers.

We already have a live instance of exactly this class of bug: the production dashboard
default (lb20 / 1.5σ) is stale and is currently promoted on the front page.

Mitigation is mechanical, not procedural:

1. Every file with `status: authoritative` **must** have a `verify_test`.
2. That test asserts the file's central quantitative claim against a fresh run.
   Example: `test_wti_brent_claims.py` asserts that a backtest of offset-0 at
   lb20/1.5σ still produces a Sharpe below the "no robust edge" threshold the file
   claims, and that offset-1 in the stable region still clears it.
3. CI fails on a broken claim. A failing verify_test **downgrades the file to `draft`
   automatically** — the loader checks the last CI status, and draft files get the
   caveat banner.

If you cannot write a test for a claim, the claim is qualitative and the file should
say so explicitly rather than dressing it in a number.

---

## What does NOT go in here

- **Numbers the model should quote.** Numbers come from the payload, not from prose in
  these files. If a number appears in an MD file it is *illustrative*, and it will be
  stripped by the grounding guard if the model repeats it. That is intended.
- **Trade recommendations.** These files explain mechanics. Sizing and execution are
  Chen's.
- **Anything from the `energy/` personal library that has not been deliberately cleared
  for work product.** See `platform/architecture.md` on the IP boundary.

---

## Status of this scaffold

Everything in this folder is **DEMO CONTENT written to establish the shape**. The
structure, front-matter schema, and layer contracts are the deliverable. The prose
inside each methodology file is a starting point drawn from work already done — it
needs Mark's review before any file moves off `status: draft`.

Search for `TODO(mark)` for the specific spots that need a decision.

---
id: platform_architecture
title: Platform architecture and IP boundary
pages: []
flags: []
status: draft
owner: mark
last_verified: 2026-07-14
verify_test: null
---

# Platform architecture (context for maintainers, not loaded into prose)

`pages: []` — this file is **not** fed to the model. It is here so the knowledge folder is
self-documenting for whoever picks the system up next.

## Stack

- FastAPI backend, port 8000
- React frontend, port 3000
- PostgreSQL (FlowsDB / us_db_dev), port 5432

## Where the AI layer sits

The AI panel is additive and sits *beside* the existing page endpoints. It introduces no
new data path: `GET /api/pages/{page}/context` composes from the same services the React
cards already call.

If a number cannot be reached by an existing endpoint, the panel cannot produce it. That
is a design property, not a limitation to be engineered around.

## Explainability constraint

Every component of this platform must be interpretable to a discretionary trader. The
platform competes with Bridgeton on *transparency*, not sophistication. This rules out any
approach where the answer to "why does it say that" is "the model decided."

The AI panel does not relax this constraint — it is subject to it. Hence: deterministic
detection, model explanation, visible provenance, numeric grounding guard.

## IP boundary

- `energy/` — personal library. Methodology only, no data committed.
- App layer (FastAPI / React / this knowledge folder) — work product.

Strategies developed in work context do not flow back into the personal library without a
deliberate decision. The knowledge folder is work product and stays on the work side.

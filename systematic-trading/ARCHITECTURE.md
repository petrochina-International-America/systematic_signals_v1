# SystematicTrading — Architecture & Build Log

This document records the architectural decisions made on 2026-06-22 when
the project was restructured from a monolithic Dash app into a decoupled
API + React frontend.

---

## The Problem

The original Dash app was **monolithic** — one Python process loaded data from
FlowsDB, ran strategy math, built Plotly figures server-side, and served HTML
to the browser. This created three bottlenecks:

1. **No multi-user support.** The server-side LRU cache in `data/lab.py` is
   process-global and keyed by params alone — two users running different
   backtests share the same cache with no session isolation.

2. **UI customization hits a wall.** Every new interactive control in Dash
   requires a Python callback chain. Circular dependencies between stores
   and selectors required workarounds (e.g. `dcc.persistence` on the commodity
   selector to avoid a store↔selector loop). CSS overrides for Dash's
   RC-slider and Select components were fragile.

3. **Tight coupling.** Data logic and presentation logic lived in the same
   process. Adding a new visualization meant touching both Python data code
   and Python layout code — no separation of concerns.

## The Solution

Split the app into three layers:

```
┌──────────────────────────────────────────────────────┐
│  React Frontend  (localhost:3000)                    │
│  Builds charts client-side, manages UI state         │
│  Calls the API via fetch()                           │
└──────────────┬───────────────────────────────────────┘
               │  HTTP / JSON
┌──────────────▼───────────────────────────────────────┐
│  FastAPI Layer  (localhost:8000)                      │
│  Thin translation: HTTP requests → data module calls │
│  Serializes pandas → JSON, returns responses         │
│  Zero business logic                                 │
└──────────────┬───────────────────────────────────────┘
               │  Python function calls
┌──────────────▼───────────────────────────────────────┐
│  Data Layer  (unchanged)                             │
│  data/loader.py  — FlowsDB prices, in-memory cache  │
│  data/lab.py     — strategy runners, LRU cache       │
│  data/cot.py     — COT positioning (synthetic)       │
│  data/signals.py — live signal snapshot              │
│  energy/*        — strategy engines                  │
└──────────────────────────────────────────────────────┘
```

The Dash app (`app.py` on port 8050) still works unchanged — it calls the
same `data/*` modules directly. Both UIs can run simultaneously.

---

## What Was Built

### Phase 1: FastAPI API Layer (`api/`)

**Goal:** Expose the existing `data/*` modules as JSON endpoints so any
frontend can consume them.

**Key design decision: split by data domain, not by page.** This means a
React page can pull data from multiple domains without new backend work.
For example, if the Signals page wants to show COT positioning alongside
momentum signals, it just calls `/api/cot/{commodity}` — no new endpoint
needed.

**Files created:**

| File | Purpose |
|------|---------|
| `api/main.py` | FastAPI app — lifespan startup (warms data store), CORS middleware, router wiring |
| `api/serialize.py` | Pandas → JSON conversion: `df_to_timeseries`, `series_to_dict`, `df_to_records`, `grid_to_heatmap`, `serialize_lab_result` |
| `api/market_data.py` | `/api/market-data/` — commodity list, forward curves, front month, aligned prices |
| `api/signals.py` | `/api/signals/` — live momentum/carry snapshot (reformats tuple-keyed dict to JSON) |
| `api/cot.py` | `/api/cot/` — positioning history, snapshot, follow-the-flow, fade-the-crowd signals |
| `api/lab.py` | `/api/lab/` — backtest runs, cached results, diagnostics, split metrics, parameter sweeps |

**19 endpoints total.** Every endpoint is a thin wrapper — call the data
module, serialize the result, return JSON. The API contains zero strategy
math or business logic.

**Serialization conventions** (consistent across all endpoints):

| Data shape | JSON format |
|------------|-------------|
| Time series (DatetimeIndex DataFrame) | `{"dates": [...], "columns": {"col": [...], ...}}` |
| Single series (DatetimeIndex Series) | `{"dates": [...], "values": [...]}` |
| Records (flat table) | `[{"col1": val, ...}, ...]` |
| Heatmap (2-D grid) | `{"x": [...], "y": [...], "z": [[...], ...]}` |
| Metrics (scalars) | `{"Sharpe": 0.85, "CAGR": 0.08, ...}` |

NaN/Inf/NaT → `null` in all cases. Dates are ISO strings.

### Phase 2: React Frontend (`frontend/`)

**Goal:** Port the entire Dash UI to React, consuming the FastAPI endpoints.

**Tech stack:**
- Vite 8 (build tooling, dev server with API proxy)
- React 19
- react-plotly.js (charts built client-side)
- React Router (page navigation)
- Vanilla CSS (dark theme ported from Dash's `assets/style.css`)

**Key architectural decisions:**

1. **Charts build client-side.** The Dash app built Plotly figures in Python
   (`components/charts.py`, `components/lab_charts.py`) and sent finished
   figure JSON to the browser. The React app receives raw data arrays from
   the API and constructs Plotly trace/layout objects in JavaScript
   (`src/charts/cotCharts.js`, `src/charts/labCharts.js`). Same visual
   output, but the frontend controls rendering — resize, zoom, and hover
   work without server round-trips.

2. **Shared state via React Context.** The commodity selector state that
   Dash managed through `dcc.Store("commodity-store")` + persistence is
   now a React context (`useCommodity`) backed by `sessionStorage`. Simpler,
   no circular dependency issues.

3. **URL state for Strategy Lab.** Lab parameters are synced to URL search
   params via `useSearchParams` — same bookmarkable behavior as the Dash
   version, but using React Router instead of `dcc.Location`.

4. **Native HTML controls.** Dash's `dcc.Dropdown` (React-Select) and
   `dcc.Slider` (RC-Slider) required extensive CSS overrides for the dark
   theme. The React app uses native `<select>` and `<input type="range">`
   elements styled with CSS — fewer dependencies, cleaner appearance.

**Files created:**

```
frontend/
  vite.config.js              — proxy /api → localhost:8000
  src/
    api/client.js              — fetch wrappers for all 19 endpoints
    hooks/useCommodity.jsx     — commodity context + sessionStorage
    hooks/useApi.jsx           — generic async fetch hook
    charts/theme.js            — dark colors, Plotly layout base, config
    charts/cotCharts.js        — COT chart builders (positioning, sentiment, histogram)
    charts/labCharts.js        — Lab chart builders (price space, spread, MTM, sweep)
    components/
      Sidebar.jsx              — nav links with active state (NavLink)
      Topbar.jsx               — page title + commodity selector
      MetricCard.jsx           — KPI card (label, value, subtitle, color)
      SignalGrid.jsx           — strategy × commodity grid with click handlers
      DataTable.jsx            — dark-themed table with conditional coloring
      Panel.jsx                — table-panel and chart-panel wrappers
      Loading.jsx              — loading/error placeholder states
    pages/
      CotFlows.jsx             — 4 metric cards, 3 charts, positioning table
      Signals.jsx              — basket + group grids, clickable drill-down
      Levels.jsx               — shell page (fake data, unchanged)
      StrategyLab.jsx          — full controls for all 4 strategies, sweep, tables
    styles/global.css          — dark theme (ported from Dash)
```

**All four pages ported:** COT Flows, Signals, Levels, Strategy Lab.
The Signals page drill-down calls `/api/lab/run` to compute a backtest
on click — same behavior as the Dash pattern-matching callback version.

---

## How to Run

```bash
# Terminal 1 — FastAPI (from systematic-trading/)
py -3.14 -m uvicorn api.main:app --reload --port 8000

# Terminal 2 — React dev server (from systematic-trading/frontend/)
npx vite --port 3000

# Then open http://localhost:3000
```

The Dash app still works independently:
```bash
py -3.14 app.py    # port 8050
```

---

## Data Flow: Before vs After

### Before (Dash)
```
Browser click → Dash callback (Python)
  → data module computes result (pandas)
  → callback builds Plotly figure (Python)
  → Dash serializes figure → browser renders it
```

### After (React + API)
```
Browser click → React component calls fetch()
  → FastAPI route → data module computes result (pandas)
  → serialize.py converts DataFrame → JSON
  → JSON sent to browser
  → React builds Plotly traces (JavaScript)
  → react-plotly.js renders the chart
```

**What changed:** presentation logic moved from server to client.
**What didn't change:** all data logic, strategy engines, FlowsDB connection,
caching behavior.

---

---

## Publishing to FlowsDB (added 2026-07-23)

Until now nothing was persisted — every number was computed in-process and
served over HTTP. `data/publish.py` adds a write path so the shared work
dashboard can read the Signals and Levels tabs out of FlowsDB instead of
calling this app.

### Where it sits in the pipeline

```
Bloomberg ──► prices_daily ──► compute signals ──► systematic.* ──┬─► dashboard
              (other repo)     (data.publish)      (same FlowsDB)  └─► us_db_dev
```

Compute sits **between** the price load and the replication fan-out. Anywhere
else and the shared DB serves signals derived from an older price pull than the
prices sitting next to them — which nothing would flag.

Three rules the scheduler has to honour:

1. **Trigger on the price load finishing, not on a clock.** `as_of_date` is
   derived from `MAX(date)` in the price store, so firing early doesn't corrupt
   anything — it just republishes yesterday and still records `status='ok'`.
   Pass `--require-fresh` (CLI) or `?require_fresh=true` (HTTP) to turn that
   into a loud 409 instead of a silent no-op.
2. **Gate replication on `publish_run.status = 'ok'`** so a half-written or
   failed publish never reaches us_db_dev. Reading through the `systematic.v_*`
   views does this automatically — they join `v_latest_date`, which only
   considers successful runs.
3. **A compute failure must not block the price leg.** Prices are independent
   and matter more; the fan-out for `prices_daily` should not depend on this
   stage succeeding.

### Invocation

| | |
|---|---|
| `py -3.14 -m data.publish --require-fresh` | batch; needs this repo + its Python env on the host |
| `POST /api/admin/publish?require_fresh=true` | needs only HTTP; re-warms the price store first |

Roughly 2 minutes either way (~18s to re-pull and pivot `prices_daily`, the rest
in the ~106 canonical backtests). Set a generous client timeout on the HTTP form.

### Cache invalidation

`data/lab.py` keys its result cache by normalized params **only** — there is no
data-generation component — so a price refresh does not invalidate it. Any code
path that re-pulls prices must call `lab.clear_caches()` or it will keep serving
backtests computed on the previous pull. `_repull()` in `api/main.py` is the one
place that does this; both admin endpoints go through it.

### What is deliberately not published

`market-data` (it *is* `prices_daily`, re-pivoted — republishing it would be
circular) and the open-ended `lab` / `sizing` parameter space. Only
`data.publish.canonical_runs()` is persisted; widen that list to publish more.

---

## Known Limitations & Next Steps

1. **COT data is synthetic.** `data/cot.py` generates seeded random walks.
   When the `cot_bbg` DB table is built, only `_fetch_cot_bbg()` changes —
   the API endpoints and React pages stay the same.

2. **Levels page is a shell.** Hardcoded sample rows. Needs the confluence
   engine to be built.

3. **Single-process cache.** The LRU in `data/lab.py` is process-global.
   For multi-user, this needs per-session keying or an external cache
   (Redis). The API layer makes this easier to add — it's a middleware
   concern, not a frontend concern.

4. **Plotly.js bundle size.** The production build includes the full
   plotly.js library (~4.9MB). Can be reduced with partial imports
   (`plotly.js-dist-min` or custom bundle with only the trace types used).

5. **No authentication.** CORS is wide-open (`allow_origins=["*"]`).
   For production/multi-user, add auth middleware and restrict CORS origins.

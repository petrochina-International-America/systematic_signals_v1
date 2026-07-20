# SystematicTrading — Design Notes

Living design doc for *this* app (the company-facing live dashboard).
Captures decisions and the reasoning behind them so they survive
iterations and don't need to be re-litigated each session.

## Repo split — what's what

- **`systematic-trading/`** (this repo) — the company-facing live app:
  signals monitor, COT flows, levels. This is what gets built out and
  eventually shipped/handed off.
- **`Systematic_Energy_Trading-main/`** — personal research snapshot
  (school project origin). Kept as reference/testing ground; not the
  thing being productionized directly.
- **`energy/`** — the signal-generation library ("the brain"). Lives as
  a **sibling directory**, referenced via `sys.path` insert in `app.py`
  (see lines ~4-9), *not* copied into this repo.

### Why `energy` is referenced, not vendored
1. It's personal IP from school — keeping it structurally separate
   preserves a clean boundary. When a more senior person eventually
   wants this in production, "does the company get the source, or just
   access to its outputs" stays an open, explicit question rather than
   something that already happened by default because the code sat in
   the company repo.
2. One copy, edited in place — as the app grows it'll need new entry
   points into `energy` (e.g. "run this strategy on a synthetic/perturbed
   price series" for simulation). Editing one shared copy avoids drift
   between a "research version" and an "app version."
3. When the time comes to formalize (proper `pyproject.toml`, private
   repo, pip-installable), the clean boundary means no untangling first
   — just add packaging metadata to a folder that's already separate.
   **Decision: don't build that packaging now — do it when there's
   someone to hand it to.**

## Data pipeline architecture

### In-memory loader (current implementation)
```
FlowsDB prices_daily
   → data/loader.py  ← one batch pull at startup, held in RAM
        _prices: {commodity → wide F1..F24 df}
        _expiry: {ticker_root → DatetimeIndex}
        TTL: 4h auto-refresh, explicit warm_up() at startup
   → data/prices.py  (thin delegate — no DB queries)
   → data/signals.py → energy library (momentum, carry)
   → Dash pages (snapshot cached 5 min)
```
`app.py` calls `loader.warm_up()` once at startup. After that, no DB
queries run during page loads — everything hits the in-memory store.
Fails loudly on startup if FlowsDB is unreachable.

**Expiry calendars:** loaded from `expiry_calendars.xlsx` (path
configured via `EXPIRY_CALENDAR_PATH` in `.env`). Currently points to
the copy in `Systematic_Energy_Trading-main/`; update the env var when
a standalone copy is placed in `systematic-trading/data/`.

Two distinct paths for latency-sensitive vs. exploratory use:

### 1. Precomputed / batch path (for "live" monitor views)
```
FlowsDB prices_daily  →  loader.warm_up() at startup
   → energy (momentum, carry)  →  signal snapshot
   → Dash (thin reads from in-memory cache — no SQL in callbacks)
```
Runs once at startup, then re-runs every 4h (loader TTL) + 5-min
signal cache. Nothing expensive runs in a Dash callback on this path.

Future evolution: daily scheduled job writes signals back to FlowsDB
output tables; Dash becomes a pure SQL read. The loader layer stays the
same — signals.py just reads from DB instead of computing live.

### 2. Live / scenario path (for "what-if" simulation)
```
Trader input (hand-edited price, curve shock, bullish/bearish scenario)
   → energy (direct import, run live, in-process)
   → Dash (rendered immediately — bypasses FlowsDB entirely)
```
There's nothing to precompute here — the input is hypothetical and
will never exist in FlowsDB. This is the one place where calling
`energy` directly inside a Dash callback is correct. To stay
interactive, scope it tightly: one commodity / one strategy / a
recent window — not a full multi-asset historical backtest per
slider tick.

## Signals page — layout decisions

### Flat Price strategies in scope
**Momentum and Carry only.** Value (F12 deviation from long-run mean)
exists in `energy.strategies.value` but is excluded from the monitor
for now — it doesn't behave well enough to surface to traders yet.
If/when it's rehabilitated it slots straight in as a third column.

Long Rolling is a benchmark, not a signal — never shown in the monitor.

### Top-level split: Flat Price vs. Spreads
Two separate sections/pages under Signals, **not** one shared grid,
because the row identity is fundamentally different between them:

- **Flat Price** (Momentum, Carry): a signal is "Long/Short/Flat
  *this commodity*." Rows = product groups / individual commodities.
- **Spreads** (Product Spread, Time Spread): a signal is
  "Long/Short *this pair/curve point*" — two legs, not one instrument.
  Rows = spread pairs, not commodities.

Cramming both into one commodity-by-strategy grid forces a row
taxonomy mismatch (a "WTI" row doesn't mean the same thing in a
flat-price column as it would in a spread column). Splitting the
*views* keeps each grid internally coherent — that's **less**
conceptual complexity even though it's nominally two pages.

### Within Spreads: combine at the page level, not the grid level
One "Spreads" page/nav entry (navigational simplicity), but it
contains **two separate grids** stacked — Product Spreads, then Time
Spreads — because their row taxonomies differ too:
- Product Spread row = a *commodity pair* (e.g. WTI–Brent, or a crack
  spread like RBOB–WTI)
- Time Spread row = a *single commodity's curve point* (e.g. WTI F1–F2)

This mirrors the cover→drill-down pattern already used on Flat Price
(see below) — apply the same shape to each spread type rather than
inventing a third layout.

### Signal cell design — flat price vs. spreads differ
**Flat price cell**: direction chip only — Long / Short / Flat.
The signal IS the binary position (+1/0/-1 from MA crossover or curve
slope). No meaningful continuous "strength" score exists that's
comparable across strategies and commodities at the cover level.

**Spread cell**: direction chip + σ strength sub-label (e.g. "+2.3σ").
The spread signal is derived from how many standard deviations the
spread is from its historical mean — that continuous σ is meaningful,
comparable across pairs, and worth surfacing at the cover. Binary
Long/Short is still the primary thing a trader reads; σ is the "how
strongly" context below it. Click through for the full detail view.

### The cover → drill-down pattern (established on Flat Price, reuse everywhere)
1. **Cover / monitor**: equal-weighted *basket* per product group —
   one row per group, scannable at a glance for cross-sectional
   patterns ("everything's short crude this week").
2. **Drill-down**: scroll down to per-group sections showing
   individual commodities (or pairs, for spreads) at the same
   strategy-column grid shape.
3. **Click-through** (not yet built): clicking a row opens a detail
   view — price/spread chart, backtest stats, and scenario/what-if
   controls scoped to that one (item, strategy).

This shape was chosen because expansion is expected mainly in the
*commodity/product* dimension (more rows), not the *strategy*
dimension (columns stay ~3-5) — a grid scales gracefully in rows.

## Current build status (as of this writing)
- `data/loader.py` — **done**: batch pull from `prices_daily`, in-memory
  store, 4h TTL refresh, expiry calendar loading. Called by `app.py` at startup.
- `data/prices.py` — **done**: thin delegate to `loader`, no DB queries.
- `data/signals.py` — **done**: uses `loader.get_prices()` / `loader.get_expiry()`,
  runs `energy` momentum + carry strategies, 5-min signal cache, basket aggregation.
- `components/signal_grid.py` — reusable clickable grid + chip component, dark-themed.
- `pages/signals.py` — Flat Price monitor (basket cover + per-group drill-down) built.
  Spreads page **not yet built**.
- Click-through to a detail/chart/backtest/scenario view — **not yet built**.

## Near-term TODOs (rough order)
1. **Copy `expiry_calendars.xlsx` into `systematic-trading/data/`** and update
   `EXPIRY_CALENDAR_PATH` in `.env` to `data/expiry_calendars.xlsx` — this cuts the
   last dependency on `Systematic_Energy_Trading-main/`.
2. **Test live signals** — run `app.py`, confirm loader warms up, signals page shows
   real Long/Short/Flat rather than "—" for the commodities with Bloomberg data.
3. **Build click-through detail view** — price chart (F1 history), backtest equity,
   scenario/what-if controls for one (commodity, strategy).
4. **Spreads page** — define row taxonomy (pairs/tenors), dual grids (Product Spreads
   + Time Spreads), direction + σ strength cells.
5. **Scheduled write-back** — daily job writes signals to FlowsDB output tables;
   `signals.py` becomes a thin SQL read rather than running strategies live.

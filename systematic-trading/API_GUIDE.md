# SystematicTrading API — How It Works

This guide explains the FastAPI layer that sits between your data/strategy
engines and any frontend (React, mobile, another service, or even just
`curl` in your terminal).

---

## Quick Start

```bash
# From the systematic-trading/ directory:
py -3.14 -m pip install fastapi uvicorn[standard]
py -3.14 -m uvicorn api.main:app --reload --port 8000
```

Then open **http://localhost:8000/docs** in your browser — FastAPI auto-generates
an interactive page where you can try every endpoint without writing any code.

The Dash app (`py -3.14 app.py` on port 8050) keeps working as before.
The API runs alongside it on a separate port.

---

## What Is an API?

An API (Application Programming Interface) is a set of URLs that a program
can call to get data or trigger actions.  Instead of a human clicking around
a dashboard, code sends an HTTP request to a URL and gets back JSON
(structured text that code can easily parse).

**Example:** your browser visits `http://localhost:8000/api/market-data/commodities`
and gets back:

```json
{"commodities": ["Brent", "Butane", "Ethane", "Natgas", ...]}
```

A React app does the same thing with `fetch()` and then renders
the list as a dropdown, grid, or whatever UI you want.

---

## How It's Organized

The API is split by **data domain**, not by page.  This is the key design
decision — it means pages can mix and match data from any domain without
needing new backend work.

```
/api/
├── health              GET    — is the server alive?
├── market-data/        GET    — prices, curves, commodity list
├── signals/            GET    — live momentum / carry snapshot
├── cot/                GET    — COT positioning + signal helpers
└── lab/                POST   — strategy backtests, sweeps, diagnostics
```

### Why domains, not pages?

If the Signals page today shows momentum + carry, and tomorrow you want to
add a COT positioning column, you just call `/api/cot/{commodity}` from the
frontend.  No new endpoint needed — the data is already there.

---

## Endpoints Reference

### Market Data — `/api/market-data/`

These endpoints serve price data from the in-memory store (FlowsDB).

| Method | Path | What it returns |
|--------|------|-----------------|
| GET | `/commodities` | List of all commodity names loaded from FlowsDB |
| GET | `/prices/{commodity}` | Full forward curve F1..F24 (date-indexed time series) |
| GET | `/prices/{commodity}/front` | Front-month (F1) close series |
| POST | `/prices/aligned` | Multiple commodities on their shared trading calendar |

**Query parameters** (optional):
- `start_date` — ISO date like `2020-01-01` (filters the series)
- `end_date` — ISO date
- `normalized` — `true` to get $/bbl-equivalent prices (for strategy engines)

**Example:**
```
GET /api/market-data/prices/WTI?start_date=2023-01-01
```
```json
{
  "commodity": "WTI",
  "normalized": false,
  "dates": ["2023-01-03", "2023-01-04", ...],
  "columns": {
    "F1": [76.93, 77.10, ...],
    "F2": [77.05, 77.22, ...],
    ...
  }
}
```

**For the aligned endpoint**, send a POST with a JSON body:
```json
{"commodities": ["WTI", "Brent"]}
```

---

### Signals — `/api/signals/`

| Method | Path | What it returns |
|--------|------|-----------------|
| GET | `/snapshot` | Current momentum/carry direction for every commodity + basket |

**Response structure:**
```json
{
  "product_groups": {
    "Crudes": ["WTI", "Brent", "Murban", ...],
    "Products": ["ULSD", "RBOB", ...],
    ...
  },
  "strategies": ["Momentum", "Carry"],
  "signals": {
    "WTI": {"Momentum": "Long", "Carry": "Short"},
    "Brent": {"Momentum": "Flat", "Carry": "Long"},
    "Crudes": {"Momentum": "Long", "Carry": "Flat"},
    ...
  }
}
```

The `signals` dict contains both individual commodities AND product-group
baskets (like "Crudes") — the basket is a majority-vote aggregate.

---

### COT — `/api/cot/`

| Method | Path | What it returns |
|--------|------|-----------------|
| GET | `/status` | Whether data is synthetic (`{"synthetic": true}`) |
| GET | `/snapshot?commodities=WTI,Brent` | Latest row per commodity (positioning table) |
| GET | `/{commodity}` | Full positioning history + latest summary |
| GET | `/{commodity}/follow-the-flow?fast=4&slow=16` | MA-crossover signal series |
| GET | `/{commodity}/fade-the-crowd?threshold_pct=20` | Sentiment-index contrarian signal |

**Signal endpoints** return the computed signal alongside the raw data, so the
frontend can plot both the underlying positioning and the derived signal:

```json
{
  "commodity": "WTI",
  "signal_type": "follow_the_flow",
  "params": {"fast": 4, "slow": 16},
  "data": {
    "dates": ["2015-01-06", ...],
    "columns": {
      "mm_net": [170000, ...],
      "ma_fast": [168000, ...],
      "ma_slow": [165000, ...],
      "signal": [1.0, ...]
    }
  }
}
```

---

### Strategy Lab — `/api/lab/`

This is the compute-heavy section.  Backtest results are cached server-side
so repeated requests with the same parameters are instant.

| Method | Path | What it returns |
|--------|------|-----------------|
| GET | `/strategies` | Strategy metadata (tiers, pairs, signals, defaults) |
| GET | `/commodities` | Lab-ready commodities (those with roll configs) |
| GET | `/commodities/{commodity}/fcols` | Available F-columns for carry leg selection |
| **POST** | `/run` | Run a backtest — returns full result |
| GET | `/result/{key}` | Re-fetch a cached result by key |
| GET | `/diagnostics/{key}` | Full/Pre/Post-Ukraine diagnostics table |
| GET | `/split-metrics/{key}` | Year-by-year sample-split analytics |
| **POST** | `/sweep` | Run a 2-D parameter sweep (Sharpe heatmap) |

#### Running a backtest

**POST** `/api/lab/run` with a JSON body.  Only include the fields you want
to change — everything else gets sensible defaults:

```json
{
  "strategy": "Momentum",
  "commodity": "WTI",
  "tier": "Fast"
}
```

Or for stat-arb:
```json
{
  "strategy": "Stat-Arb",
  "pair": "WTI / Brent",
  "lookback": 60,
  "entry": 1.0,
  "exit": 0.0,
  "hedge": "50/50"
}
```

**The response** contains everything the frontend needs to render charts
and tables — no second request needed:

```json
{
  "key": "{\"strategy\": \"Momentum\", ...}",
  "kind": "directional",
  "strategy": "Momentum",
  "commodity": "WTI",
  "label": "WTI Momentum — Fast",

  "price_space": {
    "dates": ["2015-01-02", ...],
    "columns": {
      "daily_pnl": [...],
      "cum_pnl": [...],
      "cum_net_pnl": [...]
    }
  },
  "price_space_metrics": {
    "Sharpe": 0.85,
    "Total PnL": 4520.30,
    ...
  },

  "mtm": {
    "dates": [...],
    "columns": {
      "capital": [...],
      "equity_index": [...],
      "daily_pnl": [...]
    }
  },
  "mtm_metrics": {
    "Sharpe": 0.72,
    "CAGR": 0.08,
    "Drawdown": -0.15,
    "Total PnL": 125000
  },

  "position": {"dates": [...], "values": [1.0, 1.0, -1.0, ...]},
  "held_price_native": {"dates": [...], "values": [76.5, 77.1, ...]}
}
```

The `key` is a JSON string you can save and pass to `/result/{key}`,
`/diagnostics/{key}`, or `/split-metrics/{key}` later without recomputing.

#### Parameter sweeps

**POST** `/api/lab/sweep`:

```json
{"strategy": "Momentum", "commodity": "WTI"}
```

Returns a heatmap-ready payload:

```json
{
  "x": [5, 10, 15, 20, 30, 60, 90, 120, 180, 250],
  "y": [1, 2, 3, 5, 10, 20, 30, 40, 60],
  "z": [[null, 0.5, 0.6, ...], ...],
  "x_title": "Slow MA (days)",
  "y_title": "Fast MA (days)",
  "cur_x": 60,
  "cur_y": 5,
  "title": "WTI Momentum — Price-Space Sharpe (fast × slow)"
}
```

This maps directly to a Plotly heatmap trace — `x`, `y`, `z` are exactly
what `react-plotly.js` expects.

---

## Data Format Conventions

Every endpoint follows the same serialization rules so the frontend code
can use consistent helpers:

| Data shape | JSON format | When used |
|------------|-------------|-----------|
| **Time series** (DataFrame with date index) | `{"dates": [...], "columns": {"col1": [...], ...}}` | Prices, PnL series, COT history, signals |
| **Single series** (Series with date index) | `{"dates": [...], "values": [...]}` | Position, held price |
| **Records** (flat table) | `[{"col1": val, "col2": val}, ...]` | Snapshots, diagnostics tables |
| **Heatmap** (2-D grid) | `{"x": [...], "y": [...], "z": [[...], ...]}` | Parameter sweeps |
| **Metrics** (scalar dict) | `{"Sharpe": 0.85, "CAGR": 0.08, ...}` | Price-space / MTM metrics |

**NaN, Inf, NaT → `null`** in all cases.  Dates are always ISO strings (`"2024-01-02"`).

---

## How This Connects to a React Frontend

The future React app replaces only the Dash **presentation layer**.  The flow:

```
React component
    ↓  fetch("/api/market-data/prices/WTI")
FastAPI endpoint
    ↓  calls data.loader.get_prices("WTI")
In-memory store (loaded from FlowsDB at startup)
    ↓  returns pandas DataFrame
Serializer
    ↓  df_to_timeseries(df)
JSON response back to React
    ↓
react-plotly.js renders the chart
```

**What changes vs. the Dash app:**
- Dash built Plotly figures server-side → React builds them client-side
- Dash used `dcc.Store` to pass cache keys → React uses the `key` from `/api/lab/run`
- Dash callbacks reacted to selector changes → React components call `fetch()` on user interaction

**What stays the same:**
- All data logic: `data/loader.py`, `data/lab.py`, `data/cot.py`, `data/signals.py`
- All strategy engines: `energy/strategies/*`
- The FlowsDB connection and caching

---

## File Structure

```
systematic-trading/
  api/
    __init__.py         ← makes api/ a Python package
    main.py             ← FastAPI app: startup, CORS, wires up routers
    serialize.py        ← pandas → JSON conversion helpers
    market_data.py      ← /api/market-data/ routes
    signals.py          ← /api/signals/ routes
    cot.py              ← /api/cot/ routes
    lab.py              ← /api/lab/ routes
  data/                 ← unchanged — the business logic layer
    loader.py           ← FlowsDB prices, in-memory cache
    lab.py              ← strategy runners, LRU cache, sweeps
    cot.py              ← COT data (synthetic for now)
    signals.py          ← live signal snapshot
    prices.py           ← thin accessor over loader
    db.py               ← SQLAlchemy engine
  app.py                ← Dash app (unchanged, still works on :8050)
```

The `api/` layer is **purely a translator** — it calls into `data/*`, converts
the pandas results to JSON, and returns them.  Zero business logic lives in
the API routes.

---

## Adding New Data to a Page

This is the workflow for "I want the Signals page to also show COT positioning":

1. **Check if the data already exists as an endpoint.**
   In this case, `/api/cot/{commodity}` already returns positioning data.

2. **In your React component**, add a second `fetch()` call:
   ```js
   const [signals, setSignals] = useState(null);
   const [cot, setCot] = useState(null);

   useEffect(() => {
     fetch("/api/signals/snapshot").then(r => r.json()).then(setSignals);
     fetch(`/api/cot/${commodity}`).then(r => r.json()).then(setCot);
   }, [commodity]);
   ```

3. **Render the data** however you want — table, chart, badge, etc.

**No backend changes needed.**  If you need a computation that doesn't exist
yet (e.g. a new strategy), you add it to `data/` and expose it through a new
route in `api/`.

---

## The Interactive Docs

FastAPI auto-generates two documentation pages:

- **Swagger UI**: http://localhost:8000/docs — click any endpoint, fill in
  parameters, hit "Execute", and see the real response.  This is the fastest
  way to explore what's available.

- **ReDoc**: http://localhost:8000/redoc — same info in a reference-manual
  layout.

Both are generated from the Python code — the docstrings in the route
functions become the endpoint descriptions.

POST /api/lab/run
    ↓  FastAPI routes to...
api/lab.py → run_backtest(strategy="Momentum", commodity="WTI", tier="Fast")
    ↓  which calls...
data/lab.py → run_momentum(commodity, tier)
    ↓  which calls...
energy/strategies/momentum.py → compute MA crossover, generate signals, calculate PnL
    ↓  returns a DataFrame
serialize.py → converts to JSON
    ↓
{"dates": [...], "columns": {"cum_pnl": [...], ...}}
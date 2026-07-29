# Integration guide

## Daily data → read the DB

Everything the Signals/Levels tabs show, plus the ~106 canonical backtests
(equity curves, diagnostics, split metrics), is published every morning to
`systematic.*` (replicated: `us_analysts.*`). Read the `v_*` views, show
`v_latest_date` as "data as of", warn if it's older than the last weekday.
Exact SQL + payload shapes: `data/readback.py`. Proof of correctness:
`audit_artifacts/parity/`.

## Interactive part → call the live API

**Where it lives:** this app's FastAPI service, port 8002. Chain:

```
your UI → POST /api/lab/run → data/lab.py → energy/* engines → prices in memory
```

It is NOT in the DB and can't be: it computes backtests for parameters the
user picks at click-time (an unbounded space — results don't exist until
chosen).

**How it works:** stateless JSON over HTTP. You send params, it returns the
full backtest (~1s, cached by param-hash). Any frontend can call it.

| endpoint | does |
|---|---|
| `GET /api/lab/strategies`, `/commodities` | option lists + defaults to build your controls |
| `POST /api/lab/run` | backtest for chosen params → equity curve, positions, metrics |
| `GET /api/lab/diagnostics/{key}`, `/split-metrics/{key}` | detail tables for that run |
| `POST /api/lab/sweep` | 2-D param grid → Sharpe heatmap |

**What data it controls:** only the interactive views — the Strategy Lab
page and "edit parameters" on a drill-down. Params it takes: strategy,
commodity/pair, momentum tier or fast/slow MAs, carry legs, stat-arb
lookback/entry/month-offset, roll tenor, vol target/window.

**What it does NOT control:** nothing the DB serves. Signals, levels,
rankings, and the default drill-down for every card come from the DB and
render fine with this API down.

**Rule of thumb:** default view → DB. User changed a parameter → API.
Don't copy the `energy/*` engines into your repo — one compute
implementation, reached over HTTP.

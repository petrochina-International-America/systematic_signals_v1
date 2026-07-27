"""
FastAPI application for SystematicTrading.

Run from the systematic-trading/ directory:

    py -3.14 -m uvicorn api.main:app --reload --port 8000

The Dash app continues to run on :8050 as before.
This API runs alongside it on :8000 for React (or any other) frontends.
"""

import sys
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)          # systematic-trading/
_REPO_ROOT = os.path.dirname(_PROJECT_ROOT)          # h:\SystematicTrading\

sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _REPO_ROOT)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import data.loader as loader
    loader.warm_up()
    from api.signals import precompute
    precompute()
    yield


app = FastAPI(
    title="SystematicTrading API",
    description="JSON API for commodity strategy data — market data, signals, COT, and strategy lab.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


from api.market_data import router as market_data_router
from api.signals import router as signals_router
from api.cot import router as cot_router
from api.lab import router as lab_router
from api.levels import router as levels_router
from api.sizing import router as sizing_router

app.include_router(market_data_router, prefix="/api/market-data", tags=["Market Data"])
app.include_router(signals_router, prefix="/api/signals", tags=["Signals"])
app.include_router(cot_router, prefix="/api/cot", tags=["COT"])
app.include_router(lab_router, prefix="/api/lab", tags=["Strategy Lab"])
app.include_router(levels_router, prefix="/api/levels", tags=["Levels"])
app.include_router(sizing_router, prefix="/api/sizing", tags=["Sizing"])


@app.get("/api/health")
def health():
    """Liveness check — returns ok once the data store is warmed."""
    from data import loader
    return {
        "status": "ok",
        "commodities_loaded": len(loader.loaded_commodities()),
        "latest_data_date": loader.latest_data_date(),
        "pull_timestamp": loader.latest_pull_timestamp(),
    }


def _repull() -> dict:
    """
    Re-pull prices and drop every stale downstream cache.

    The signal/levels caches key off loader._loaded_at and self-invalidate once
    warm_up() bumps it, but data.lab's result cache is keyed by params alone —
    it survives a price refresh and would keep serving backtests computed on the
    previous pull. Clear it explicitly.
    """
    import data.loader as loader
    from data import lab

    loader.warm_up()
    evicted = lab.clear_caches()
    return {
        "commodities_loaded": loader.loaded_commodities(),
        "latest_data_date": loader.latest_data_date(),
        "lab_cache_evicted": evicted,
    }


@app.post("/api/admin/reload")
def force_reload():
    """Force re-warm the data store and recompute all signal caches."""
    from api.signals import precompute
    result = _repull()
    precompute()
    return {"status": "reloaded", **result}


@app.post("/api/admin/publish")
def force_publish(require_fresh: bool = False):
    """
    Refresh from FlowsDB, then publish the systematic.* snapshot.

    The scheduling hook for the Bloomberg -> prices_daily -> compute ->
    prices_daily -> {dashboard, us_db_dev} pipeline: call this once the upstream
    price load commits, and gate the downstream replication on the returned
    as_of_date (or on systematic.publish_run.status = 'ok').

    Always re-warms first — a long-running API process holds prices for up to
    _REFRESH_TTL (4h), so publishing off the in-memory store without a refresh
    would write signals computed on the previous pull.

    require_fresh=true refuses to publish when the price store has not advanced
    past the last successful publish, turning a too-early trigger into a visible
    failure instead of a silently stale snapshot.

    Synchronous, ~2 minutes — set a generous client timeout. The publish itself
    warms the signal/spread/levels caches as a side effect (it calls those same
    producers), so only the top-performers bar is warmed separately afterwards.
    """
    from fastapi import HTTPException
    from data.publish import publish

    refreshed = _repull()
    try:
        summary = publish(require_fresh=require_fresh)
    except RuntimeError as exc:
        # Staleness guard and "no price data" both land here — 409 so the
        # scheduler can distinguish "not ready yet" from a real 500.
        raise HTTPException(409, str(exc))

    # Warm the ranking bar last: it re-runs the canonical backtests, and with
    # data.lab._MAX_RESULTS at 24 against ~106 canonical runs the LRU has
    # already evicted them. Raise _MAX_RESULTS if this second pass matters.
    from api.signals import _compute_top_performers
    _compute_top_performers()

    return {"status": "published", **refreshed, **summary}

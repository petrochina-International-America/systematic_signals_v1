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


@app.post("/api/admin/reload")
def force_reload():
    """Force re-warm the data store and recompute all signal caches."""
    import data.loader as loader
    from api.signals import precompute
    loader.warm_up()
    precompute()
    return {
        "status": "reloaded",
        "commodities_loaded": loader.loaded_commodities(),
    }

"""
/api/lab — Strategy Lab: backtest runs, parameter sweeps, diagnostics.

Heavy computation (backtest, sweep) is cached server-side in data.lab's
LRU.  The API accepts parameters, runs or fetches cached results, and
returns fully serialized JSON.
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from api.serialize import (
    serialize_lab_result,
    df_to_records,
    metrics_to_dict,
    grid_to_heatmap,
)

router = APIRouter()


# ── request models ───────────────────────────────────────────────────────────

class MomentumSpeedsRequest(BaseModel):
    commodity: str = "WTI"
    vol_target: float | None = None
    vol_window: int | None = None


class LabRunRequest(BaseModel):
    strategy: str = "Momentum"
    commodity: str = "WTI"
    tier: str | None = None
    fast: int | None = None
    slow: int | None = None
    carry_front: str | None = None
    carry_end: str | None = None
    epsilon: float | None = None
    pair: str | None = None
    lookback: int | None = None
    entry: float | None = None
    exit: float | None = None       # accepted for back-compat, IGNORED (mean-cross only, 2026-07-16)
    exit_mode: str | None = None    # accepted for back-compat, IGNORED (mean-cross only, 2026-07-16)
    band_mode: str | None = None   # "zscore" (default) | "dollar" (RESEARCH)
    month_offset: int | None = None
    hedge: str | None = None
    roll_tenor: str | None = None   # "Prompt" | "Q2" | "Q3" | "1yr" ("Deferred (1Y)" aliased to "1yr")
    cot_signal: str | None = None
    cot_fast: int | None = None
    cot_slow: int | None = None
    cot_threshold: float | None = None
    vol_target: float | None = None
    vol_window: int | None = None


class SweepRequest(BaseModel):
    strategy: str
    commodity: str | None = None
    pair: str | None = None
    roll_tenor: str | None = None
    exit_mode: str | None = None   # accepted for back-compat, IGNORED (mean-cross only)
    band_mode: str | None = None   # "zscore" (default) | "dollar" (RESEARCH)
    month_offset: int | None = None


# ── metadata ─────────────────────────────────────────────────────────────────

@router.get("/strategies")
def strategies():
    """Strategy metadata: names, momentum tiers, stat-arb pairs, COT signals, roll tenors."""
    from data import lab
    from energy.accounting.spread_specs import get_spread_spec
    from energy.analytics.signal_summary import pair_defaults

    return {
        "strategies": lab.STRATEGIES,
        "momentum_tiers": {k: [list(p) for p in v] for k, v in lab.MOMENTUM_TIERS.items()},
        "stat_arb_pairs": [{"leg1": a, "leg2": b, "label": lab.pair_label(a, b),
                           "precision_mode": (get_spread_spec(a, b) or {}).get("precision_mode", "rank_approximate")}
                           for a, b in lab.STAT_ARB_PAIRS],
        # Full default-view config per pair (lookback, entry, exit_mode,
        # month_offset, cross_arb) — the SAME shared object the Signals and
        # Levels pages read; the frontend initializes its controls from this
        # so the detail page's first load cannot drift from those cards.
        "stat_arb_pair_defaults": {
            k: pair_defaults(k) for k in lab.STAT_ARB_PAIR_DEFAULTS
        },
        "roll_tenors": list(lab.ROLL_TENORS.keys()),
        "cot_signals": lab.COT_SIGNALS,
        "defaults": lab.DEFAULTS,
    }


@router.get("/commodities")
def lab_commodities():
    """Commodities with both loaded prices and a usable roll config (lab-ready)."""
    from data import lab
    return {"commodities": lab.available_commodities()}


@router.get("/commodities/{commodity}/fcols")
def available_fcols(commodity: str):
    """F-columns with data for a commodity (e.g. ["F1", "F2", ..., "F24"])."""
    from data import lab

    try:
        cols = lab.available_fcols(commodity)
    except Exception:
        raise HTTPException(404, f"No data for '{commodity}'")
    return {"commodity": commodity, "fcols": cols}


# ── backtest run ─────────────────────────────────────────────────────────────

@router.post("/run")
def run_lab(body: LabRunRequest):
    """
    Run (or fetch cached) a strategy backtest.

    Accepts the same parameters the Dash lab uses.  Unset fields get defaults.
    Returns the full result: time series, metrics, and metadata.
    """
    from data import lab

    raw = {k: v for k, v in body.model_dump().items() if v is not None}

    try:
        key = lab.run_lab(raw)
        result = lab.get_result(key)
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")

    return serialize_lab_result(key, result)


@router.get("/result/{key:path}")
def get_result(key: str):
    """Fetch a previously computed result by its cache key."""
    from data import lab

    try:
        result = lab.get_result(key)
    except Exception as e:
        raise HTTPException(404, f"Result not found or recompute failed: {e}")

    return serialize_lab_result(key, result)


# ── momentum speed comparison ────────────────────────────────────────────────

@router.post("/momentum-speeds")
def momentum_speeds(body: MomentumSpeedsRequest):
    """
    Run all 5 momentum speed tiers and return equity curves + per-period stats.

    Used by the multi-speed overlay chart on the Momentum detail page.
    Each tier gets: dates, equity_index, MA pairs, and summary stats for
    full sample plus pre/post-Ukraine sub-periods.
    """
    from data import lab
    from api.serialize import _clean
    import numpy as np
    import pandas as pd

    commodity = body.commodity
    vol_target = body.vol_target or 0.15
    vol_window = body.vol_window or 60
    split = pd.Timestamp(lab.UKRAINE_SPLIT)

    tiers_order = ["Very Fast", "Fast", "Medium", "Slow", "Averaged"]
    tiers = {}

    for tier_name in tiers_order:
        key = lab.run_lab({
            "strategy": "Momentum",
            "commodity": commodity,
            "tier": tier_name,
            "vol_target": vol_target,
            "vol_window": vol_window,
        })
        r = lab.get_result(key)
        mtm = r["mtm"]
        pos = r["position"]
        eq = mtm["equity_index"].copy()

        # Null out equity during MA warmup — before the first actual trade.
        # Positions are 0 during warmup, so the equity line sits flat at 1.0
        # which is misleading in the comparison chart. Show nothing instead.
        nonzero = pos[pos != 0]
        if not nonzero.empty:
            eq[eq.index < nonzero.index[0]] = np.nan

        def _slice_stats(eq_slice, pos_slice):
            eq_v = eq_slice.dropna()
            if len(eq_v) < 2:
                return None
            vals = eq_v.values
            years = len(vals) / 252
            rets = np.diff(vals) / vals[:-1]
            cagr = float((vals[-1] / vals[0]) ** (1 / years) - 1) if years > 0 else 0.0
            vol = float(np.std(rets) * np.sqrt(252))
            s = float(np.mean(rets) / np.std(rets) * np.sqrt(252)) if np.std(rets) > 0 else 0.0
            peak = np.maximum.accumulate(vals)
            max_dd = float(np.min(vals / peak - 1))
            pv = pos_slice.values
            trades = sum(1 for i in range(1, len(pv)) if pv[i] != pv[i - 1] and pv[i] != 0)
            return {
                "cagr": round(cagr, 3),
                "vol_ann": round(vol, 3),
                "sharpe": round(s, 2),
                "max_dd": round(max_dd, 3),
                "trades_yr": int(round(trades / years)) if years > 0 else 0,
                "trades_wk": round(trades / (years * 52), 1) if years > 0 else 0,
            }

        full = _slice_stats(eq, pos)
        pre = _slice_stats(eq[eq.index < split], pos[pos.index < split])
        post = _slice_stats(eq[eq.index >= split], pos[pos.index >= split])

        tiers[tier_name] = {
            "dates": [d.isoformat()[:10] for d in mtm.index],
            "equity_index": [_clean(v) for v in eq.values],
            "ma_pairs": [list(p) for p in lab.MOMENTUM_TIERS[tier_name]],
            "sharpe": full["sharpe"] if full else 0,
            "trades_yr": full["trades_yr"] if full else 0,
            "trades_wk": full["trades_wk"] if full else 0,
            "periods": {"2015-22": pre, "22-26": post},
        }

    return {"commodity": commodity, "tiers": tiers}


# ── diagnostics tables ───────────────────────────────────────────────────────

@router.get("/diagnostics/{key:path}")
def diagnostics(key: str):
    """
    MTM diagnostics split by Full Sample / Pre-Ukraine / Post-Ukraine.

    Returns a flat records table.
    """
    from data import lab

    try:
        result = lab.get_result(key)
    except Exception as e:
        raise HTTPException(404, str(e))

    diag_df = lab.diagnostics(result)
    return {"data": df_to_records(diag_df)}


@router.get("/split-metrics/{key:path}")
def split_metrics(key: str):
    """
    Sample-split analytics: Full / Pre-Ukraine / Post-Ukraine + year-by-year.

    Returns a flat records table with PS:: and MTM:: prefixed columns.
    """
    from data import lab

    try:
        result = lab.get_result(key)
    except Exception as e:
        raise HTTPException(404, str(e))

    split_df = lab.split_metrics(result).round(3)
    return {"data": df_to_records(split_df)}


# ── parameter sweeps ─────────────────────────────────────────────────────────

@router.post("/sweep")
def run_sweep(body: SweepRequest):
    """
    Run a 2-D parameter sweep (Sharpe heatmap).

    - Momentum: fast MA × slow MA grid (requires commodity).
    - Stat-Arb: lookback × entry threshold grid (requires pair, optional roll_tenor).

    Returns Plotly-ready heatmap data: x[], y[], z[][], axis titles, cursor position.
    """
    from data import lab

    # Pass only the caller-set fields: sweep_for resolves the rest from
    # DEFAULTS plus the shared per-pair config (pre-merging DEFAULTS here
    # would mask which fields were actually unset).
    params = {k: v for k, v in body.model_dump().items() if v is not None}

    pack = lab.sweep_for(params)
    if pack is None:
        raise HTTPException(400, f"No sweep available for strategy '{body.strategy}'")

    grid, info = pack
    return grid_to_heatmap(grid, info)

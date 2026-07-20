"""
/api/sizing — Live daily position sizing.

POST /api/sizing/today
    Runs (or fetches cached) the strategy backtest, reads the last vol scalar
    and position signal, and returns trader-ready lot counts with VaR estimates.

The endpoint re-uses the same cached backtest result that the lab UI is already
displaying — no extra computation.  Vol scalar comes from result["mtm"]["vol_scalar"]
so it is numerically identical to what the backtest plotted.

Prices in the request body are the trader's live reference prices.  They are
NOT auto-updated; provide them explicitly or rely on SIZING_CONFIGS defaults
which must be reviewed manually when price levels shift significantly.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class SizingRequest(BaseModel):
    # ── Backtest params (same as LabRunRequest) ───────────────────────────────
    strategy: str = "Momentum"
    commodity: str | None = None      # single-leg strategies (Momentum, Carry)
    pair: str | None = None           # pair strategies (Stat-Arb)
    tier: str | None = None
    lookback: int | None = None
    entry: float | None = None
    exit: float | None = None       # accepted for back-compat, IGNORED (mean-cross only)
    exit_mode: str | None = None    # accepted for back-compat, IGNORED (mean-cross only)
    band_mode: str | None = None   # "zscore" (default) | "dollar" (RESEARCH)
    month_offset: int | None = None
    hedge: str | None = None
    roll_tenor: str | None = None
    vol_target: float | None = None
    vol_window: int | None = None

    # ── Sizing-specific ───────────────────────────────────────────────────────
    # When None, falls back to SIZING_CONFIGS defaults (set at calibration time).
    capital_base: float | None = None
    ref_price: float | None = None         # single-leg: today's live price
    leg1_ref_price: float | None = None    # pair leg1: today's live price
    leg2_ref_price: float | None = None    # pair leg2: today's live price
    lot_rounding: float = 1.0
    sizing_mode: str = "gaussian"          # "gaussian" or "empirical_var"


@router.post("/today")
def todays_size(body: SizingRequest):
    """
    Return today's position size for a strategy.

    Pulls the cached backtest result, reads the last vol scalar and position,
    applies the sizing formula, and returns lot counts + 1-day 95% VaR.

    sizing_mode="gaussian"     — VaR = notional × realized_vol × 1.645
    sizing_mode="empirical_var" — VaR = abs(5th percentile of last vol_window
                                          days of realized dollar P&L)
    """
    import math
    import numpy as np
    from data import lab
    from energy.sizing.daily_size import (
        CAPITAL_BASE,
        get_todays_size,
        get_todays_size_pair,
        SIZING_CONFIGS,
    )

    # Build lab params (exclude sizing-specific fields)
    _SIZING_FIELDS = {
        "capital_base", "ref_price", "leg1_ref_price", "leg2_ref_price",
        "lot_rounding", "sizing_mode",
    }
    lab_params = {
        k: v for k, v in body.model_dump().items()
        if v is not None and k not in _SIZING_FIELDS
    }

    try:
        key = lab.run_lab(lab_params)
        result = lab.get_result(key)
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")

    mtm = result["mtm"]
    position_series = result["position"]

    # Most recent signal — already lagged in the backtest (t-1 data)
    signal = float(position_series.iloc[-1])

    # Vol scalar — forward-filled in measures.py so last row is always valid
    vol_scalar = float(mtm["vol_scalar"].iloc[-1])

    # Realized vol — only written on rebalance days (NOT forward-filled), so
    # .iloc[-1] can be NaN if the last date wasn't a rebalance.  Use dropna().
    if "realized_vol_ann" in mtm.columns:
        rv_series = mtm["realized_vol_ann"].dropna()
        if not rv_series.empty:
            realized_vol_daily = float(rv_series.iloc[-1]) / math.sqrt(252.0)
        else:
            realized_vol_daily = float("nan")
    else:
        realized_vol_daily = float("nan")

    as_of_date = mtm.index[-1].strftime("%Y-%m-%d") if len(mtm) > 0 else ""

    is_pair = body.strategy in ("Stat-Arb", "RV")

    # Best available price from the backtest itself (actual held price, updated
    # daily). Used when the caller doesn't supply a live price override.
    btm_ref_price = None
    if "ref_price" in mtm.columns:
        rp = mtm["ref_price"].dropna()
        if not rp.empty:
            btm_ref_price = float(rp.iloc[-1])

    if is_pair:
        pair_key = body.pair or ""
        cfg = SIZING_CONFIGS.get(body.strategy, {}).get(pair_key, {})

        capital_base  = body.capital_base  or cfg.get("capital_base", CAPITAL_BASE)
        # For pairs the backtest ref_price is the leg1/leg2 average; we still
        # fall back to calibration defaults since we can't split it back out.
        leg1_price    = body.leg1_ref_price or cfg.get("leg1_ref_price",  80.0)
        leg2_price    = body.leg2_ref_price or cfg.get("leg2_ref_price",  82.0)
        leg1_mult     = cfg.get("leg1_multiplier", 1_000)
        leg2_mult     = cfg.get("leg2_multiplier", 1_000)

        parts     = pair_key.split(" / ")
        leg1_name = parts[0] if len(parts) == 2 else "Leg1"
        leg2_name = parts[1] if len(parts) == 2 else "Leg2"

        hist_dollar_pnl = None
        if body.sizing_mode == "empirical_var" and "dollar_pnl" in mtm.columns:
            vol_w = body.vol_window or cfg.get("vol_window", 120)
            hist_dollar_pnl = mtm["dollar_pnl"].dropna().values[-vol_w:]

        payload = get_todays_size_pair(
            signal=signal,
            capital_base=capital_base,
            leg1_price=leg1_price,
            leg2_price=leg2_price,
            leg1_mult=leg1_mult,
            leg2_mult=leg2_mult,
            leg1_name=leg1_name,
            leg2_name=leg2_name,
            vol_scalar=vol_scalar,
            realized_vol_daily=realized_vol_daily,
            lot_rounding=body.lot_rounding,
            hist_dollar_pnl=hist_dollar_pnl,
            sizing_mode=body.sizing_mode,
            as_of_date=as_of_date,
        )
        return payload

    else:
        commodity = body.commodity or "WTI"
        cfg = SIZING_CONFIGS.get(body.strategy, {}).get(commodity, {})

        capital_base       = body.capital_base or cfg.get("capital_base", CAPITAL_BASE)
        # Price priority: caller-supplied → backtest last held price → calibration default.
        # Backtest last held price is the actual rolled settlement price so it reflects
        # today's market, making the notional and VaR numbers meaningful.
        ref_price          = body.ref_price or btm_ref_price or cfg.get("ref_price", 80.0)
        contract_multiplier = cfg.get("contract_multiplier", 1_000)
        leg_fraction        = cfg.get("leg_fraction",        1.0)

        hist_dollar_pnl = None
        if body.sizing_mode == "empirical_var" and "dollar_pnl" in mtm.columns:
            vol_w = body.vol_window or cfg.get("vol_window", 120)
            hist_dollar_pnl = mtm["dollar_pnl"].dropna().values[-vol_w:]

        return get_todays_size(
            signal=signal,
            capital_base=capital_base,
            ref_price=ref_price,
            contract_multiplier=contract_multiplier,
            vol_scalar=vol_scalar,
            realized_vol_daily=realized_vol_daily,
            leg_fraction=leg_fraction,
            lot_rounding=body.lot_rounding,
            hist_dollar_pnl=hist_dollar_pnl,
            sizing_mode=body.sizing_mode,
            as_of_date=as_of_date,
        )

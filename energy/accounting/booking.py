# energy/accounting/booking.py
"""
The sanctioned per-unit P&L booking primitive.

Every per-unit (price-space / sweep) P&L series in this repo books on the
capital loops' timing: the position earning day t's flow is the one held
INTO t, i.e.

    P&L[t] = position[t-1] * flow[t]        (weights[t-1] per leg, if any)

which is the per-unit twin of the dollar loops' contracts[t-1] * flow[t].
Booking position[t] * flow[t] instead credits a position with the move of
the very close its signal was decided from (same-close optimistic — the
convention retired on 2026-07-09 after it inflated the stat-arb sweep
layer by a day relative to the capital loop).

held_pnl() below is the ONLY place this alignment is written for per-unit
layers. The dollar capital loops implement the same convention internally
(they need capital compounding, rebalance sizing, and vol scaling that a
per-unit series has no business knowing about); they are held to it by
tests/test_booking_convention.py, which asserts each loop's per-lot
extraction equals held_pnl() output exactly.
"""
from __future__ import annotations

import pandas as pd


def held(position: pd.Series) -> pd.Series:
    """
    The position held INTO each day t: position[t-1], flat on the first day.

    This is the one sanctioned lag between a decision series and the flows
    it earns. Any per-unit P&L or cost line that scales by "the position
    that was actually on" must go through held()/held_pnl(), never an
    inline shift.
    """
    return position.shift(1).fillna(0.0)


def held_pnl(
    position: pd.Series,
    flows: pd.Series | pd.DataFrame,
    weights: pd.DataFrame | None = None,
    *,
    zero_first: bool = True,
    fill_value: float | None = None,
) -> pd.Series:
    """
    Per-unit strategy P&L on the capital-loop convention.

    position : decision series ({-1,0,+1} or continuous); day t's value is
        what was decided AT t's close. held_pnl applies it from t+1.
    flows : Series of per-unit flows (single instrument or pre-combined
        spread flow), or DataFrame of per-leg flows combined with `weights`.
    weights : per-leg weight frame for DataFrame flows, as DECIDED (day t's
        row = weights set at t's close). Shifted one day internally and
        aligned to the flows index — weights[t-1] scale flow[t], matching
        the N-leg loop's contracts set at the prior rebalance. The row sum
        uses skipna=False: a day with any missing leg weight books NaN (then
        `fill_value`), never a partial basket.
    zero_first : force the first row to 0.0 — the loops book no P&L on
        their own day 0 (they size at that close and earn from the next
        day). Pass False only to reproduce a legacy series byte-for-byte.
    fill_value : optional fill for NaN products (e.g. 0.0 where a caller
        previously ended with .fillna(0)); None preserves NaNs.
    """
    pos_held = held(position)

    if isinstance(flows, pd.DataFrame):
        if weights is None:
            raise ValueError("DataFrame flows require a weights frame.")
        w_prev = weights.shift(1).reindex(flows.index)
        combined = (w_prev * flows).sum(axis=1, skipna=False)
    else:
        if weights is not None:
            raise ValueError("weights only apply to DataFrame flows.")
        combined = flows

    out = pos_held * combined
    if fill_value is not None:
        out = out.fillna(fill_value)
    if zero_first and len(out):
        out.iloc[0] = 0.0
    return out

"""
Risk parity portfolio construction.

Two levels of API:

1. inverse_vol_weights(returns_dict, vol_window)
   → {name: weight} dict.  Lightweight, stateless — suitable for
   snapshot views (e.g. the Proximity Scanner's blended CTA position).

2. build_risk_parity_portfolio(results, members, ...)
   → full backtest with rolling rebalance, same output structure as
   build_measures (price_space, mtm, metrics).  Uses inverse-vol
   weights recomputed at each rebalance date.

Weighting: w_i = (1/σ_i) / Σ(1/σ_j)  where σ_i is the trailing
realized vol of strategy i's daily returns.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ── Snapshot: current weights ───────────────────────────────────────────────

def inverse_vol_weights(
    returns: dict[str, pd.Series],
    vol_window: int = 120,
    min_obs: int = 20,
) -> dict[str, float]:
    """
    Inverse-volatility weights from trailing realized vol.

    Parameters
    ----------
    returns : dict[str, pd.Series]
        {name: daily return series} for each strategy/asset.
        Series need not be aligned — each uses its own trailing window.
    vol_window : int
        Lookback in trading days for realized vol estimate.
    min_obs : int
        Minimum observations required; strategies with fewer are excluded.

    Returns
    -------
    dict[str, float]
        {name: weight} summing to 1.0.  Empty dict if no valid inputs.
    """
    inv_vols: dict[str, float] = {}
    for name, rets in returns.items():
        tail = rets.dropna()
        if len(tail) < min_obs:
            continue
        tail = tail.iloc[-vol_window:]
        vol = float(tail.std(ddof=1))
        if vol > 1e-8:
            inv_vols[name] = 1.0 / vol

    if not inv_vols:
        return {}

    total = sum(inv_vols.values())
    return {name: iv / total for name, iv in inv_vols.items()}


# ── Full backtest: rolling risk-parity portfolio ────────────────────────────

def build_risk_parity_portfolio(
    results: dict[str, dict],
    members: list[str],
    *,
    vol_window: int = 120,
    initial_capital: float = 1_000_000,
    rebalance_freq: str = "ME",
) -> dict:
    """
    Build a risk-parity portfolio from individual build_measures results.

    Each leg's MTM daily_ret is a capital % return (already individually
    vol-targeted).  At each rebalance date the weights are set to
    inverse-vol on the trailing window; between rebalances the weights
    are held constant.

    Parameters
    ----------
    results : dict
        {commodity_name: build_measures_output} for each strategy.
    members : list[str]
        Which names to include.  Missing names are skipped.
    vol_window : int
        Trailing window for the inverse-vol calculation.
    initial_capital : float
        Starting capital for the portfolio MTM account.
    rebalance_freq : str
        Pandas offset alias for rebalance schedule (default "ME" = month-end).

    Returns
    -------
    dict — same structure as build_measures:
        price_space, price_space_metrics, mtm, mtm_metrics, weights_history
    """
    from energy.accounting.measures import build_measures

    leg_returns: dict[str, pd.Series] = {}
    for name in members:
        if name not in results:
            continue
        r = results[name]["mtm"]["daily_ret"].dropna()
        if not r.empty:
            leg_returns[name] = r

    if not leg_returns:
        raise ValueError("No valid members found in results.")

    active = list(leg_returns)

    common = leg_returns[active[0]].index
    for name in active[1:]:
        common = common.intersection(leg_returns[name].index)
    common = common.sort_values()

    if common.empty:
        raise ValueError(f"Intersection calendar is empty across: {active}")

    aligned = pd.DataFrame({n: leg_returns[n].reindex(common) for n in active})

    # Rebalance on the LAST trading day of each period — true month-end for
    # the default, regardless of weekends/holidays — plus day 0 for initial
    # weights.  Offset aliases are normalized to Period-compatible freqs
    # ("ME" raises in to_period on pandas >= 2.2).  The previous
    # to_period().to_timestamp() set produced period-START stamps and only
    # matched when the 1st was a trading day, silently skipping the
    # rebalance in every month that opens on a weekend/holiday.
    period_freq = {"ME": "M", "QE": "Q", "YE": "Y"}.get(rebalance_freq, rebalance_freq)
    periods = common.to_period(period_freq)
    is_period_end = np.r_[periods[:-1] != periods[1:], True]
    rebal_dates = set(common[is_period_end])
    rebal_dates.add(common[0])

    weights = pd.DataFrame(index=common, columns=active, dtype=float)
    current_w = pd.Series(1.0 / len(active), index=active)

    for t in common:
        if t in rebal_dates:
            # Estimation window ends at t-1: the weight DECIDED at t's close
            # must not see day t's own return (same held convention as the
            # capital loops' vol scalars, which size at t from returns[:t]).
            lookback = aligned.loc[:t].iloc[:-1].iloc[-vol_window:]
            w = inverse_vol_weights(
                {n: lookback[n] for n in active},
                vol_window=vol_window,
            )
            if w:
                current_w = pd.Series(w)
        weights.loc[t] = current_w

    # weights_history row t = the weight decided AT t's close; day t's return
    # is earned by the weight held INTO t (weights[t-1]) — the capital-loop
    # convention (energy.accounting.booking). Applying weights[t] to ret[t]
    # let a weight earn the very return that participated in setting it
    # (D2 divergence, fixed 2026-07-10). Day 0 books nothing, like the loops.
    portfolio_ret = (aligned * weights.shift(1)).sum(axis=1).fillna(0.0)

    ref_ones = pd.Series(1.0, index=portfolio_ret.index)

    result = build_measures(
        daily_pnl=portfolio_ret,
        ref_price=ref_ones,
        contract_multiplier=1.0,
        vol_window=0,
        initial_capital=initial_capital,
    )
    result["weights_history"] = weights
    return result

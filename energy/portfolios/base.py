"""
Equal-weight portfolio construction from individual build_measures results.

Each leg contributes its MTM daily_ret (already a capital % return from
vol-targeted sizing). These are directly comparable across commodities
without any price normalization — they are cash streams in % terms.

The portfolio return is the equal-weight average on the intersection
calendar (handles ICE vs CME date differences). No second vol-targeting
layer is applied; the legs are already individually scaled.

Usage:
    from energy.portfolios.base import build_portfolio
    from energy.portfolios.groupings import NGL_MEMBERS

    ngl_mom = build_portfolio(
        momentum_results, NGL_MEMBERS,
        initial_capital=CAPITAL,
    )
    # Same output structure as build_measures:
    #   price_space, price_space_metrics, mtm, mtm_metrics
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from energy.accounting.measures import build_measures


def build_portfolio(
    results: dict[str, dict],
    members: list[str],
    *,
    initial_capital: float = 1_000_000,
) -> dict:
    """
    Build an equal-weight portfolio from individual build_measures results.

    Each leg's MTM daily_ret is a capital % return — already vol-targeted
    and directly comparable as a cash stream. The portfolio simply averages
    these on the intersection calendar and compounds them into a capital
    account. No second vol-targeting pass is applied.

    Parameters
    ----------
    results : dict
        {commodity_name: build_measures_output} for each commodity.
    members : list[str]
        Commodities to include. Any name missing from results is skipped
        with a warning.
    initial_capital : float
        Starting capital for the portfolio MTM account.

    Returns
    -------
    dict — same structure as build_measures:
        price_space, price_space_metrics, mtm, mtm_metrics
    """
    # ── 1. Collect MTM daily returns (already capital % returns) ─────────────
    leg_returns: dict[str, pd.Series] = {}
    for name in members:
        if name not in results:
            print(f"[portfolio] {name} not in results — skipping")
            continue
        r = results[name]["mtm"]["daily_ret"].dropna()
        if r.empty:
            print(f"[portfolio] {name} produced empty returns — skipping")
            continue
        leg_returns[name] = r

    if not leg_returns:
        raise ValueError("No valid members found in results.")

    active = list(leg_returns)

    # ── 2. Intersection calendar (handles ICE vs CME date differences) ────────
    common = leg_returns[active[0]].index
    for name in active[1:]:
        common = common.intersection(leg_returns[name].index)
    common = common.sort_values()

    if common.empty:
        raise ValueError(f"Intersection calendar is empty across members: {active}")

    # ── 3. Equal-weight average on intersection dates ─────────────────────────
    aligned       = pd.DataFrame({n: leg_returns[n].reindex(common) for n in active})
    portfolio_ret = aligned.mean(axis=1)

    # ── 4. Compound into a capital account (no vol-targeting — already scaled) ─
    ref_ones = pd.Series(1.0, index=portfolio_ret.index)

    return build_measures(
        daily_pnl           = portfolio_ret,
        ref_price           = ref_ones,
        contract_multiplier = 1.0,
        vol_window          = 0,          # disable: legs are already vol-targeted
        initial_capital     = initial_capital,
    )

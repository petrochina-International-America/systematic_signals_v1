"""
/api/signals — Live signal snapshots for outrights and spreads.

Outright snapshot: momentum/carry direction + conviction per commodity.
Spread snapshot: mean-reversion direction + z-score + % from mean per pair.

Results are cached and only recomputed when the underlying price data refreshes.
"""

import json as _json
from fastapi import APIRouter, Query
from fastapi.responses import Response

router = APIRouter()

# Cache: (loader._loaded_at, result) — invalidates when prices refresh
_snapshot_cache: tuple[float, dict] = (0.0, {})
_spread_cache: dict[tuple[float, int, float], dict] = {}
_top_cache: tuple[float, list] = (0.0, [])
_top_alltime_cache: tuple[float, list] = (0.0, [])


def precompute():
    """Warm all signal caches. Call after loader.warm_up()."""
    signal_snapshot()
    spread_snapshot()
    _compute_top_performers()


@router.get("/snapshot")
def signal_snapshot():
    """
    Current momentum/carry direction + conviction for every commodity.

    Returns:
        product_groups:  {group_name: [commodity, ...]}
        strategies:      ["Momentum", "Carry"]
        signals:         {commodity: {strategy: {direction, conviction}}}
    """
    global _snapshot_cache
    from data.signals import PRODUCT_GROUPS, STRATEGIES
    from data import loader
    from energy.analytics.signal_summary import outright_snapshot
    from energy.accounting.contract_specs import CONTRACT_SPECS

    if _snapshot_cache[0] == loader._loaded_at and _snapshot_cache[1]:
        return _snapshot_cache[1]

    signals = {}
    for group, commodities in PRODUCT_GROUPS.items():
        for commodity in commodities:
            spec = CONTRACT_SPECS.get(commodity)
            if spec is None:
                signals[commodity] = {
                    "Momentum": {"direction": "—", "conviction": None},
                    "Carry": {"direction": "—", "conviction": None},
                }
                continue
            try:
                prices = loader.get_prices(commodity)
                # Momentum card reads the CLEAN roll-aware cumsum series
                # (raw generic columns jump at re-rank and bias the MA —
                # 2026-07-14 signal-series fix); carry stays on quoted prices.
                try:
                    from data.lab import _clean_signal_prices
                    sig_prices = _clean_signal_prices(commodity)
                except Exception:
                    sig_prices = None
                snap = outright_snapshot(commodity, prices, "F1",
                                         prices_signal=sig_prices)
                signals[commodity] = {
                    "Momentum": snap["Momentum"],
                    "Carry": snap["Carry"],
                }
            except Exception:
                signals[commodity] = {
                    "Momentum": {"direction": "—", "conviction": None},
                    "Carry": {"direction": "—", "conviction": None},
                }

    result = {
        "product_groups": PRODUCT_GROUPS,
        "strategies": STRATEGIES,
        "signals": signals,
    }
    _snapshot_cache = (loader._loaded_at, result)
    return result


@router.get("/spreads")
def spread_snapshot(
    lookback: int | None = Query(None, description="Rolling mean lookback in days. Omit to use each pair's calibrated default (e.g. NGL pairs use 250d, not 20d)."),
    threshold: float | None = Query(None, description="Z-score entry threshold. Omit to use each pair's calibrated default."),
):
    """
    Current mean-reversion signal for all spread pairs.

    Returns signal direction, z-score, and % deviation from rolling mean,
    grouped by spread type (Location, Cracks, NGL, Frac). Each pair uses its
    own calibrated lookback/threshold (see STAT_ARB_PAIR_DEFAULTS) unless the
    query params above explicitly override it for every pair.
    """
    global _spread_cache
    from data import loader
    from energy.accounting.contract_specs import CONTRACT_SPECS
    from energy.analytics.signal_summary import spread_snapshot as _spread_snapshot

    cache_key = (loader._loaded_at, lookback, threshold)
    if cache_key in _spread_cache:
        return _spread_cache[cache_key]

    result = _spread_snapshot(
        get_prices=loader.get_prices_normalized,
        get_expiry=loader.get_expiry,
        specs=CONTRACT_SPECS,
        lookback=lookback,
        threshold=threshold,
        get_months=loader.get_contract_months,
    )
    # Keep only the current data generation — drop stale entries
    _spread_cache = {cache_key: result}
    return result


def _sharpe_window(key, window: int | None):
    """Compute annualized Sharpe over the last `window` days (None = full sample)."""
    from data import lab
    import numpy as np

    try:
        result = lab.get_result(key)
        eq = result["mtm"]["equity_index"]
        vals = eq.dropna().values
        if len(vals) < 126:
            return None
        tail = vals[-min(window, len(vals)):] if window else vals
        rets = np.diff(tail) / tail[:-1]
        std = rets.std()
        if std == 0 or not np.isfinite(std):
            return None
        sharpe = float(rets.mean() / std * np.sqrt(252))
        return sharpe if np.isfinite(sharpe) else None
    except Exception:
        return None


# Groups excluded from the top-performers bar only: not yet verified (no
# confirmed FlowsDB price history / construction check). They still appear
# in the main Signals grid via PRODUCT_GROUPS / SPREAD_GROUPS — this
# exclusion is local to the ranking bar. Currently empty: all groups rank.
_UNVERIFIED_PRODUCT_GROUPS = set()
_UNVERIFIED_SPREAD_GROUPS = set()


def _compute_top_performers():
    """Compute and cache ranked strategy performance for 1Y and all-time.

    Skips _UNVERIFIED_PRODUCT_GROUPS / _UNVERIFIED_SPREAD_GROUPS — those
    groups are still shown in the main Signals grid, just excluded from this
    ranking bar until verified.
    """
    global _top_cache, _top_alltime_cache
    from data import loader, lab
    from data.signals import PRODUCT_GROUPS
    from energy.analytics.signal_summary import SPREAD_GROUPS

    if _top_cache[0] == loader._loaded_at and _top_cache[1]:
        return _top_cache[1], _top_alltime_cache[1]

    scores_1y = []
    scores_all = []

    momentum_tiers = ["Very Fast", "Fast", "Medium", "Slow", "Averaged"]

    for group, commodities in PRODUCT_GROUPS.items():
        if group in _UNVERIFIED_PRODUCT_GROUPS:
            continue
        for commodity in commodities:
            # Carry — single default run
            try:
                key = lab.run_lab({"strategy": "Carry", "commodity": commodity})
                result = lab.get_result(key)
                direction = "Long" if result["position"].iloc[-1] > 0 else "Short"
                base = {"commodity": commodity, "strategy": "Carry",
                        "label": f"{commodity} Carry", "direction": direction}

                s1y = _sharpe_window(key, 252)
                if s1y is not None:
                    scores_1y.append({**base, "sharpe_1y": round(s1y, 2)})
                s_all = _sharpe_window(key, None)
                if s_all is not None:
                    scores_all.append({**base, "sharpe_all": round(s_all, 2)})
            except Exception:
                pass

            # Momentum — each speed tier separately
            for tier in momentum_tiers:
                try:
                    key = lab.run_lab({"strategy": "Momentum", "commodity": commodity, "tier": tier})
                    result = lab.get_result(key)
                    direction = "Long" if result["position"].iloc[-1] > 0 else "Short"
                    base = {"commodity": commodity, "strategy": "Momentum",
                            "label": f"{commodity} Mom ({tier})", "direction": direction}

                    s1y = _sharpe_window(key, 252)
                    if s1y is not None:
                        scores_1y.append({**base, "sharpe_1y": round(s1y, 2)})
                    s_all = _sharpe_window(key, None)
                    if s_all is not None:
                        scores_all.append({**base, "sharpe_all": round(s_all, 2)})
                except Exception:
                    pass

    for group, pairs in SPREAD_GROUPS.items():
        if group in _UNVERIFIED_SPREAD_GROUPS:
            continue
        for leg1, leg2 in pairs:
            pair = f"{leg1} / {leg2}"
            try:
                key = lab.run_lab({"strategy": "Stat-Arb", "pair": pair})
                result = lab.get_result(key)
                pos = result["position"].iloc[-1]
                direction = "Long" if pos > 0 else ("Short" if pos < 0 else "Flat")
                base = {"commodity": pair, "strategy": "Stat-Arb",
                        "label": f"{pair} Mean Rev", "direction": direction}

                s1y = _sharpe_window(key, 252)
                if s1y is not None:
                    scores_1y.append({**base, "sharpe_1y": round(s1y, 2)})

                s_all = _sharpe_window(key, None)
                if s_all is not None:
                    scores_all.append({**base, "sharpe_all": round(s_all, 2)})
            except Exception:
                pass

    scores_1y.sort(key=lambda x: x["sharpe_1y"], reverse=True)
    scores_all.sort(key=lambda x: x["sharpe_all"], reverse=True)
    scores_1y = _best_momentum_per_commodity(scores_1y)
    scores_all = _best_momentum_per_commodity(scores_all)
    _top_cache = (loader._loaded_at, scores_1y)
    _top_alltime_cache = (loader._loaded_at, scores_all)
    return scores_1y, scores_all


def _best_momentum_per_commodity(scores):
    """Keep only the best-ranked momentum tier per commodity.

    `scores` must already be sorted best-first, so the first momentum entry
    seen for a commodity is its best tier; the other four are dropped.
    Without this, one strong commodity's five tiers crowd out the ranking
    bar. Non-momentum strategies pass through untouched.
    """
    seen = set()
    out = []
    for item in scores:
        if item["strategy"] == "Momentum":
            if item["commodity"] in seen:
                continue
            seen.add(item["commodity"])
        out.append(item)
    return out


def _clean_list(items):
    import math
    result = []
    for item in items:
        clean = {k: (None if isinstance(v, float) and not math.isfinite(v) else v)
                 for k, v in item.items()}
        result.append(clean)
    return result


@router.get("/top-performers")
async def top_performers(n: int = Query(5, description="Number of top strategies to return")):
    """Top N strategies by 1-year and all-time Sharpe across all outrights and spreads."""
    import traceback
    try:
        scores_1y, scores_all = _compute_top_performers()
        payload = {
            "top_1y": _clean_list(scores_1y[:n]),
            "top_alltime": _clean_list(scores_all[:n]),
        }
        return Response(content=_json.dumps(payload), media_type="application/json")
    except Exception:
        return Response(
            content=_json.dumps({"error": traceback.format_exc()}),
            media_type="application/json",
            status_code=500,
        )

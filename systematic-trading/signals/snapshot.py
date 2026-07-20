"""
Snapshot builder — assembles the SignalSnapshot that signals/flags.py evaluates.

This is the only module in the package that touches the data layer, and it
composes from the same services the existing pages already call (data.loader,
data.lab, energy.analytics.signal_summary). It introduces no new data path.

Mirrors the leaderboard's coverage (api/signals.py::_compute_top_performers):
the same strategy runs, the same unverified-group exclusions — the flags
describe what the page shows, so the snapshot covers what the page covers.
"""

import json

import numpy as np
import pandas as pd

from signals.flags import LiveSignal, SignalSnapshot, StrategyRow

# Same exclusions as the top-performers bar: groups without a confirmed
# FlowsDB price history / construction check don't produce strategy rows.
# Currently empty (mirrors api/signals.py): all groups produce rows.
_UNVERIFIED_PRODUCT_GROUPS = set()
_UNVERIFIED_SPREAD_GROUPS = set()

_MOMENTUM_TIERS = ["Very Fast", "Fast", "Medium", "Slow", "Averaged"]


def _count_trades(position: pd.Series) -> int | None:
    """
    n_trades = count of direction changes, not exits to flat.

    Each entry into a new nonzero direction is one trade, so a long→short
    flip without passing through flat counts the short as a new trade (the
    round trip is two trades, not one).
    """
    if position is None or len(position) == 0:
        return None
    sign = np.sign(pd.Series(position).fillna(0.0).to_numpy(dtype=float))
    n = 0
    prev = 0.0
    for s in sign:
        if s != 0 and s != prev:
            n += 1
        prev = s
    return n


def _sharpe_window(result: dict, window: int | None) -> float | None:
    """Annualized Sharpe over the last `window` days (None = full sample).

    Same math as api/signals.py::_sharpe_window — kept here so this package
    doesn't depend on the API layer (the API layer depends on this one).
    """
    try:
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
        return round(sharpe, 2) if np.isfinite(sharpe) else None
    except Exception:
        return None


def _direction(result: dict) -> str:
    pos = result["position"].iloc[-1]
    return "Long" if pos > 0 else ("Short" if pos < 0 else "Flat")


def _row(params: dict, pair_or_commodity: str, label: str) -> StrategyRow | None:
    from data import lab

    try:
        key = lab.run_lab(params)
        result = lab.get_result(key)
        return StrategyRow(
            strategy=params["strategy"],
            pair_or_commodity=pair_or_commodity,
            label=label,
            direction=_direction(result),
            sharpe_1y=_sharpe_window(result, 252),
            sharpe_all=_sharpe_window(result, None),
            n_trades=_count_trades(result["position"]),
            config=json.loads(key),    # normalized params = the live config
        )
    except Exception:
        return None


def _as_of():
    """Latest pull timestamp from prices_daily, tz-aware."""
    from datetime import datetime
    from data import loader

    ts = loader.latest_pull_timestamp()
    if ts is None:
        return None
    dt = datetime.fromisoformat(str(ts))
    if dt.tzinfo is None:
        dt = dt.astimezone()    # naive DB timestamp → assume system local tz
    return dt


def build_snapshot() -> SignalSnapshot:
    from data.signals import PRODUCT_GROUPS
    from data.lab import pair_label
    from energy.analytics.signal_summary import SPREAD_GROUPS

    strategies: list[StrategyRow] = []

    for group, commodities in PRODUCT_GROUPS.items():
        if group in _UNVERIFIED_PRODUCT_GROUPS:
            continue
        for commodity in commodities:
            row = _row({"strategy": "Carry", "commodity": commodity},
                       commodity, f"{commodity} Carry")
            if row:
                strategies.append(row)
            for tier in _MOMENTUM_TIERS:
                row = _row({"strategy": "Momentum", "commodity": commodity,
                            "tier": tier},
                           commodity, f"{commodity} Mom ({tier})")
                if row:
                    strategies.append(row)

    for group, pairs in SPREAD_GROUPS.items():
        if group in _UNVERIFIED_SPREAD_GROUPS:
            continue
        for leg1, leg2 in pairs:
            pair = pair_label(leg1, leg2)
            row = _row({"strategy": "Stat-Arb", "pair": pair},
                       pair, f"{pair} Mean Rev")
            if row:
                strategies.append(row)

    live_signals = tuple(_live_signals())

    return SignalSnapshot(
        as_of=_as_of(),
        strategies=tuple(strategies),
        live_signals=live_signals,
    )


def _live_signals() -> list[LiveSignal]:
    """Current momentum/carry direction+conviction per commodity — the same
    cards the Signals page renders."""
    from data import loader
    from data.signals import PRODUCT_GROUPS
    from energy.accounting.contract_specs import CONTRACT_SPECS
    from energy.analytics.signal_summary import outright_snapshot

    out: list[LiveSignal] = []
    for group, commodities in PRODUCT_GROUPS.items():
        for commodity in commodities:
            if CONTRACT_SPECS.get(commodity) is None:
                continue
            try:
                prices = loader.get_prices(commodity)
                # Momentum card reads the CLEAN roll-continuous series (raw
                # generics jump at re-rank and bias the MA — 2026-07-14 fix)
                try:
                    from data.lab import _clean_signal_prices
                    sig_prices = _clean_signal_prices(commodity)
                except Exception:
                    sig_prices = None
                snap = outright_snapshot(commodity, prices, "F1",
                                         prices_signal=sig_prices)
            except Exception:
                continue
            for strat in ("Momentum", "Carry"):
                cell = snap.get(strat) or {}
                out.append(LiveSignal(
                    commodity=commodity,
                    strategy=strat,
                    direction=cell.get("direction", "—"),
                    conviction=cell.get("conviction"),
                ))
    return out

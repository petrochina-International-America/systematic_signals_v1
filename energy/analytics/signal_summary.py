"""
energy/analytics/signal_summary.py — Signal state snapshot.

Computes the current signal direction and strength for outrights (momentum,
carry) and spreads (mean-reversion z-score) in a single pass.  Returns plain
dicts suitable for serialization — no Dash or FastAPI dependency.

Usage:
    from energy.analytics.signal_summary import outright_snapshot, spread_snapshot
"""

import warnings
import numpy as np
import pandas as pd


# ── Outright signals ─────────────────────────────────────────────────────────

_MOMENTUM_MA = 20
_CARRY_FRONT = "F1"
_CARRY_END = "F13"
_TRADE_START = "2015-01-01"


def _momentum_strength(prices_full: pd.DataFrame, front_col: str = "F1") -> dict:
    """Current momentum direction + MA level + % gap."""
    px = prices_full[front_col].astype(float).dropna()
    if len(px) < _MOMENTUM_MA:
        return {"direction": "—", "ma_value": None, "pct_from_ma": None}

    ma = px.rolling(_MOMENTUM_MA).mean()
    latest_px = float(px.iloc[-1])
    latest_ma = float(ma.iloc[-1])
    direction = "Long" if latest_px > latest_ma else "Short"
    pct = round((latest_px / latest_ma - 1) * 100, 1) if latest_ma != 0 else None

    return {"direction": direction, "ma_value": round(latest_ma, 2), "pct_from_ma": pct}


def _carry_strength(prices: pd.DataFrame) -> dict:
    """Current carry direction + F1−back spread for display."""
    f1 = prices.get(_CARRY_FRONT)
    f_end = prices.get(_CARRY_END)
    if f1 is None:
        return {"direction": "—", "spread": None, "spread_pct": None, "end_tenor": None}
    if f_end is None or f_end.dropna().empty:
        available = sorted(
            [c for c in prices.columns if c.startswith("F") and prices[c].notna().any()],
            key=lambda x: int(x[1:]),
        )
        if len(available) < 2:
            return {"direction": "—", "spread": None, "spread_pct": None, "end_tenor": None}
        f_end = prices[available[-1]]
        end_tenor = available[-1]
    else:
        end_tenor = _CARRY_END

    diff = f1.astype(float) - f_end.astype(float)
    valid = diff.dropna()
    if valid.empty:
        return {"direction": "—", "spread": None, "spread_pct": None, "end_tenor": None}

    spread = float(valid.iloc[-1])
    direction = "Long" if spread > 0 else "Short"
    f1_px = float(f1.astype(float).dropna().iloc[-1])
    pct = round(spread / f1_px * 100, 1) if f1_px != 0 else None

    return {"direction": direction, "spread": round(spread, 2), "spread_pct": pct, "end_tenor": end_tenor}


def outright_snapshot(
    commodity: str,
    prices_full: pd.DataFrame,
    front_col: str = "F1",
    prices_signal: pd.DataFrame | None = None,
    signal_col: str = "SIGNAL",
) -> dict:
    """
    Signal snapshot for one commodity.

    prices_signal: CLEAN roll-aware signal frame (leg flow cumsum — see
    energy.strategies.rolling.leg_signal_series). When provided, the
    momentum card reads it instead of the raw generic column: raw generics
    jump at re-rank, which biases the MA direction (2026-07-14 signal-series
    audit). Carry is same-day curve slope and stays on the quoted frame.

    Returns:
        {
            "commodity": "WTI",
            "Momentum": {"direction": "Long", "conviction": 85},
            "Carry":    {"direction": "Short", "conviction": 62},
        }
    """
    if prices_signal is not None and signal_col in prices_signal.columns:
        mom = _momentum_strength(prices_signal, signal_col)
    else:
        mom = _momentum_strength(prices_full, front_col)
    return {
        "commodity": commodity,
        "Momentum": mom,
        "Carry": _carry_strength(prices_full),
    }


# ── Spread signals ───────────────────────────────────────────────────────────

_DEFAULT_LOOKBACK = 20
_DEFAULT_THRESHOLD = 1.5

SPREAD_GROUPS = {
    "Location": [("WTI", "Brent"), ("Brent", "Dubai")],
    "Cracks":   [("Brent", "RBOB"), ("Brent", "ULSD"), ("ULSD", "WTI")],
    "FFAs":     [("TDL", "WDF")],
    "NGL":      [("Propane", "Ethane"), ("Propane", "Butane"), ("RBOB", "Butane")],
    "Frac":     [("Ethane", "Natgas")],
}

# ── THE single source of truth for each pair's DEFAULT VIEW ─────────────────
#
# Every surface that presents a pair's "default" stat-arb signal or backtest
# reads THIS dict (via pair_defaults() below): the Signals page cards, the
# Levels page spread panel, the Stat-Arb detail page's first load (served
# through /api/lab/strategies), data.lab.normalize_params (bare backtest
# requests), the sweep/heatmap default, and the top-performers ranking.
# Never copy these values into a consumer — read them, so the default view
# CANNOT diverge between pages (the D3 divergence class, fixed 2026-07-10).
#
# Fields:
#   lookback / entry — calibrated rolling window and z entry threshold from
#       the notebook parameter sweeps (NGL pairs mean-revert on ~250d).
#   exit_mode        — "mean_cross" (exit when spread crosses its rolling
#       mean). Since 2026-07-16 this is the ONLY exit rule the dashboard
#       and lab expose; the field stays for config-shape stability and
#       must be "mean_cross" everywhere.
#   month_offset     — the DEFAULT desired exposure (leg1 delivery month
#       relative to leg2). Distinct from SPREAD_SPECS' month_offset, which
#       defines the validated construction spec. -1 for WTI/Brent = the
#       market-convention prompt pairing (CL front is structurally one
#       delivery month before CO front — the quoted/shipped arb).
#   cross_arb        — whether the Cross-Arb exposure is a real, traded
#       market structure for this pair (renders the UI toggle). True only
#       for WTI/Brent: cracks are same-delivery-month by definition, and
#       rank-approximate pairs (NGL/gas/Dubai swaps) have no per-day
#       delivery-month tracking, so the engine ignores the offset entirely.
#   research_only    — the default config has NOT cleared the desk's
#       pre-registered 1.0 pre-cost Sharpe floor; the dashboard entry is a
#       research view, not a tradable signal. Rendered as a badge.
#
# WTI / Brent (2026-07-13 sane-config pass; numbers re-based 2026-07-14 on
# the CLEAN flow-cumsum signal series — Pass 8 found the stitched series
# biased the traded z +0.38 sigma, which inflated the previous 0.68/0.39):
# lookback 90 is DERIVED, not searched — 2xOU-half-life (48 td, CI [26,74])
# rounded to the sweep grid; entry 2.0 sigma is the operator-fixed
# threshold. On the clean signal that cell runs 0.53 full-sample / 0.34
# ex-2020&26 pre-cost per-unit Sharpe (MTM 0.44, maxDD -24%) — further BELOW
# the 1.0 floor, hence research_only stays. The old lb20/1.5 default was
# negative ex-windfall. (vol_scalar_cap removed 2026-07-16: stat-arb sizing
# is identity — the vol scalar is a constant 1.0, so there is nothing to cap.)
STAT_ARB_PAIR_DEFAULTS: dict[str, dict] = {
    "WTI / Brent":      {"lookback": 90,  "entry": 2.0, "exit_mode": "mean_cross", "month_offset": -1, "cross_arb": True,
                         "research_only": True},
    "Brent / Dubai":    {"lookback": 20,  "entry": 1.5, "exit_mode": "mean_cross", "month_offset": 0,  "cross_arb": False},
    "Brent / RBOB":     {"lookback": 20,  "entry": 1.5, "exit_mode": "mean_cross", "month_offset": 0,  "cross_arb": False},
    "Brent / ULSD":     {"lookback": 20,  "entry": 1.5, "exit_mode": "mean_cross", "month_offset": 0,  "cross_arb": False},
    "ULSD / WTI":       {"lookback": 20,  "entry": 1.5, "exit_mode": "mean_cross", "month_offset": 0,  "cross_arb": False},
    "TDL / WDF":        {"lookback": 20,  "entry": 1.5, "exit_mode": "mean_cross", "month_offset": 0,  "cross_arb": False},
    "Ethane / Natgas":  {"lookback": 20,  "entry": 1.5, "exit_mode": "mean_cross", "month_offset": 0,  "cross_arb": False},
    "Propane / Ethane": {"lookback": 250, "entry": 1.5, "exit_mode": "mean_cross", "month_offset": 0,  "cross_arb": False},
    "Propane / Butane": {"lookback": 250, "entry": 1.5, "exit_mode": "mean_cross", "month_offset": 0,  "cross_arb": False},
    "RBOB / Butane":    {"lookback": 250, "entry": 1.5, "exit_mode": "mean_cross", "month_offset": 0,  "cross_arb": False},
}

_PAIR_FALLBACK = {
    "lookback": _DEFAULT_LOOKBACK, "entry": _DEFAULT_THRESHOLD,
    "exit_mode": "mean_cross", "month_offset": 0, "cross_arb": False,
    "research_only": False,
}


def pair_defaults(pair: str) -> dict:
    """Resolved default-view config for a pair (fallback-filled copy)."""
    return {**_PAIR_FALLBACK, **STAT_ARB_PAIR_DEFAULTS.get(pair, {})}


def live_state(prev_state: float, z: float | None, threshold: float,
               exit_threshold: float = 0.0) -> float:
    """
    Advance the entry/exit state machine one step with today's z-score —
    the shared "what to do now" rule for signal cards and the Levels panel.
    Mirrors rv_zscore._build_signal exactly: entry when |z| >= threshold;
    long exits when z >= -exit_threshold, short when z <= exit_threshold
    (exit_threshold=0.0 == mean-cross; == threshold gives match-entry).
    """
    if z is None:
        return prev_state
    if prev_state == 0.0:
        if z >= threshold:
            return -1.0
        if z <= -threshold:
            return 1.0
        return 0.0
    if prev_state == 1.0:
        return 0.0 if z >= -exit_threshold else 1.0
    return 0.0 if z <= exit_threshold else -1.0


def build_pair_signal_frame(
    leg1_name: str,
    leg2_name: str,
    prices1: pd.DataFrame,
    prices2: pd.DataFrame,
    expiry1,
    expiry2,
    *,
    months1: pd.DataFrame | None = None,
    months2: pd.DataFrame | None = None,
    roll_config: str = "prompt_EOM_roll",
    lookback: int | None = None,
    threshold: float | None = None,
) -> tuple[pd.DataFrame, dict]:
    """
    The one way to build a pair's DEFAULT-VIEW signal frame: coordinated
    delivery-month legs via prepare_spread_legs (rank path only for
    rank_approximate pairs), the CLEAN flow-cumsum signal series from
    spread_signal_series, and rv_zscore._build_signal with this pair's
    config from pair_defaults(). Identical construction and parameters to
    data.lab._run_stat_arb's default run — Signals cards, Levels panel, and
    the backtest default all read the same recipe, so they cannot diverge.

    Signal-series note (Pass 8 fix, 2026-07-14): every rolling statistic
    (spread, spread_mean, spread_std, z, bands) is computed on the
    flow-cumsum series — the stitched level's relabel jumps biased the
    traded z +0.38 sigma. The stitched quoted level is still attached as a
    `quoted_spread` column for trader-facing display; it is not used in any
    signal math.

    Returns (signal_frame, resolved_config). lookback/threshold override
    the config's values for all pairs uniformly when given (comparisons).
    """
    from energy.strategies.spread_rolling import (
        prepare_spread_legs, spread_level_series, spread_signal_series)
    from energy.strategies.relative_value.rv_zscore import _build_signal

    cfg = pair_defaults(f"{leg1_name} / {leg2_name}")
    lb = int(lookback) if lookback is not None else int(cfg["lookback"])
    th = float(threshold) if threshold is not None else float(cfg["entry"])
    exit_thr = th if cfg["exit_mode"] == "match_entry" else 0.0

    leg1_df, leg2_df, meta = prepare_spread_legs(
        leg1_name, leg2_name, prices1, prices2, expiry1, expiry2,
        months1=months1, months2=months2,
        roll_config=roll_config,
        month_offset=cfg["month_offset"],
    )
    signal_spread = spread_signal_series(leg1_df, leg2_df)
    sig_df = _build_signal(signal_spread, lb, th, exit_threshold=exit_thr)
    sig_df["quoted_spread"] = spread_level_series(leg1_df, leg2_df).reindex(sig_df.index)

    resolved = {
        **cfg,
        "lookback": lb, "entry": th, "exit_threshold": exit_thr,
        "construction": meta.get("construction"),
        "precision_mode": meta.get("precision_mode"),
        "month_offset": meta.get("month_offset") if meta.get("month_offset") is not None else cfg["month_offset"],
        "signal_series": "flow_cumsum",
    }
    return sig_df, resolved


def _spread_signal(
    leg1_name: str,
    leg2_name: str,
    prices1: pd.DataFrame,
    prices2: pd.DataFrame,
    expiry1,
    expiry2,
    roll_config: str = "prompt_EOM_roll",
    lookback: int | None = None,
    threshold: float | None = None,
    months1: pd.DataFrame | None = None,
    months2: pd.DataFrame | None = None,
) -> dict:
    """
    Current mean-reversion signal for one spread pair, built by
    build_pair_signal_frame — i.e. the IDENTICAL construction, lookback,
    exit rule, and default exposure as the pair's default backtest view.

    lookback/threshold default to None, which resolves to this pair's
    config in STAT_ARB_PAIR_DEFAULTS via pair_defaults(). Pass explicit
    values to override for all pairs uniformly (comparisons).

    Returns:
        {
            "pair": "WTI / Brent",
            "direction": "Short",
            "zscore": 1.48,
            "pct_from_mean": 3.2,
            "dist_to_threshold": -0.42,
            "pct_from_threshold": -1.1,
            "lookback": 20,
            "threshold": 1.5,
            "month_offset": -1,
            "construction": "coordinated_delivery_month",
        }

    dist_to_threshold / pct_from_threshold measure distance from the *entry
    trigger band* (mean ± threshold·std), not the mean — i.e. how far the
    spread still has to move to fire (negative) or how far past the trigger
    it already is (positive). Picks whichever band (upper if deviation >= 0,
    lower if deviation < 0) the spread is actually heading toward / through.
    """
    pair = f"{leg1_name} / {leg2_name}"
    cfg = pair_defaults(pair)
    lb = lookback if lookback is not None else cfg["lookback"]
    th = threshold if threshold is not None else cfg["entry"]

    try:
        sig_df, rcfg = build_pair_signal_frame(
            leg1_name, leg2_name, prices1, prices2, expiry1, expiry2,
            months1=months1, months2=months2, roll_config=roll_config,
            lookback=lookback, threshold=threshold,
        )
    except Exception:
        return {
            "pair": pair,
            "direction": "—",
            "zscore": None,
            "spread_value": None, "spread_mean": None, "deviation": None,
            "lookback": lb, "threshold": th,
        }

    latest = sig_df.iloc[-1]
    prev_state = float(latest["signal_raw"])
    today_z = float(latest["deviation_pct"]) if pd.notna(latest["deviation_pct"]) else None
    live = live_state(prev_state, today_z, rcfg["entry"], rcfg["exit_threshold"])
    direction = "Long" if live > 0 else ("Short" if live < 0 else "Flat")

    zscore = float(latest["deviation_pct"]) if pd.notna(latest["deviation_pct"]) else None
    mean = float(latest["spread_mean"]) if pd.notna(latest["spread_mean"]) else None
    deviation = float(latest["deviation"]) if pd.notna(latest["deviation"]) else None
    spread_value = float(latest["spread"]) if pd.notna(latest["spread"]) else None
    upper_band = float(latest["upper_band"]) if pd.notna(latest["upper_band"]) else None
    lower_band = float(latest["lower_band"]) if pd.notna(latest["lower_band"]) else None
    quoted = (float(latest["quoted_spread"])
              if "quoted_spread" in sig_df.columns and pd.notna(latest["quoted_spread"])
              else None)

    # Display re-anchoring: the signal series is a flow cumsum whose LEVEL
    # drifts from the quoted spread by the accumulated roll carry. Shift all
    # trader-facing dollar levels by today's constant offset so they line up
    # with the screen quote. A constant shift leaves z, deviation, and every
    # level DIFFERENCE (e.g. dist_to_threshold) exactly unchanged — this is
    # presentation only, no signal math reads the shifted values.
    if quoted is not None and spread_value is not None:
        k = quoted - spread_value
        spread_value += k
        if mean is not None:
            mean += k
        if upper_band is not None:
            upper_band += k
        if lower_band is not None:
            lower_band += k

    pct_from_mean = None
    if mean is not None and deviation is not None and abs(mean) > 1e-8:
        pct_from_mean = round(deviation / abs(mean) * 100, 1)

    dist_to_threshold = None
    pct_from_threshold = None
    if spread_value is not None and upper_band is not None and lower_band is not None and deviation is not None:
        band = upper_band if deviation >= 0 else lower_band
        dist_to_threshold = round(spread_value - band, 2)
        if abs(band) > 1e-8:
            pct_from_threshold = round(dist_to_threshold / abs(band) * 100, 1)

    return {
        "pair": pair,
        "direction": direction,
        "zscore": round(zscore, 2) if zscore is not None else None,
        "spread_value": round(spread_value, 2) if spread_value is not None else None,
        "quoted_spread": round(quoted, 2) if quoted is not None else None,
        "spread_mean": round(mean, 2) if mean is not None else None,
        "deviation": round(deviation, 2) if deviation is not None else None,
        "pct_from_mean": pct_from_mean,
        "dist_to_threshold": dist_to_threshold,
        "pct_from_threshold": pct_from_threshold,
        "lookback": lb, "threshold": th,
        "month_offset": rcfg["month_offset"],
        "construction": rcfg["construction"],
        "precision_mode": rcfg["precision_mode"],
        "signal_series": rcfg.get("signal_series"),
    }


def spread_snapshot(
    get_prices,
    get_expiry,
    specs: dict,
    roll_config: str = "prompt_EOM_roll",
    lookback: int | None = None,
    threshold: float | None = None,
    get_months=None,
) -> dict:
    """
    Signal snapshot for all spread pairs, each built by
    build_pair_signal_frame from its own pair_defaults() config — the same
    construction the pair's default backtest view runs.

    lookback/threshold default to None, which lets each pair use its own
    calibrated default (e.g. NGL pairs mean-revert on a ~250-day window).
    Pass explicit values to force the same lookback/threshold across every
    pair instead (e.g. for an apples-to-apples comparison).

    Parameters:
        get_prices: callable(commodity) -> DataFrame (normalized prices)
        get_expiry: callable(ticker_root) -> DatetimeIndex
        specs:      CONTRACT_SPECS dict
        get_months: callable(commodity) -> CONTRACT_MONTH_YR frame; required
            for strict_delivery_match pairs (raise-or-None per pair is fine —
            a strict pair without months reports "—" rather than silently
            degrading to the disbanded rank construction)

    Returns:
        {
            "Location": [{"pair": "WTI / Brent", "direction": "Short", ...}, ...],
            "Cracks":   [...],
            ...
        }
    """
    def _months_or_none(commodity: str):
        if get_months is None:
            return None
        try:
            return get_months(commodity)
        except (KeyError, RuntimeError):
            return None

    result = {}
    for group, pairs in SPREAD_GROUPS.items():
        group_signals = []
        for leg1, leg2 in pairs:
            try:
                prices1 = get_prices(leg1)
                prices2 = get_prices(leg2)
                expiry1 = get_expiry(specs[leg1]["ticker"])
                expiry2 = get_expiry(specs[leg2]["ticker"])
            except (KeyError, RuntimeError):
                pair = f"{leg1} / {leg2}"
                cfg = pair_defaults(pair)
                group_signals.append({
                    "pair": pair, "direction": "—",
                    "zscore": None, "pct_from_mean": None,
                    "lookback": lookback if lookback is not None else cfg["lookback"],
                    "threshold": threshold if threshold is not None else cfg["entry"],
                })
                continue

            group_signals.append(_spread_signal(
                leg1, leg2, prices1, prices2, expiry1, expiry2,
                roll_config, lookback, threshold,
                months1=_months_or_none(leg1), months2=_months_or_none(leg2),
            ))
        result[group] = group_signals
    return result

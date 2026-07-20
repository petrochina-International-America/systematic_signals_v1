"""
energy/sizing/daily_size.py

Live daily position sizing for energy commodity strategies.

Extracts the single-day sizing step from each strategy's backtest capital loop
and exposes it as a standalone calculation.

The pure vol-scalar formula (_scalar_from_valid) is the single source of truth
shared with all backtest engines: measures.py, mtm.py, rv_zscore.py, and
statistical_arbitrage.py all import it instead of reimplementing the formula.

CAPITAL NOTE:
    capital_base is a RISK-SIZING DENOMINATOR, not a real capital allocation:
    it translates the vol-targeted scalar into a contract count.  It is the
    single shared CAPITAL_BASE ($1M, the research book every backtest runs
    at) for every strategy and pair, so lot counts are directly comparable
    across pairs and match the backtested position scale.  Scale lots
    linearly for a larger live allocation.  ref_price values are manual
    fallbacks — review whenever live prices drift significantly (~±20%).

Sizing formula (all strategies):
    lots = leg_fraction × vol_scalar × |signal| × capital / (price × mult)

For pair strategies (RV / Stat-Arb) each leg is independently sized to
0.5 × capital, so lot counts differ when leg prices differ — that is correct:
dollar-neutral, not lot-neutral.
"""
from __future__ import annotations

import math

import numpy as np


# ── shared vol scalar kernel ──────────────────────────────────────────────────

def _scalar_from_valid(
    valid: np.ndarray,
    vol_target_ann: float,
    vol_floor: float = 1e-8,
) -> tuple[float, float]:
    """
    Pure vol-scalar formula — single source of truth shared with backtest engines.

    Parameters
    ----------
    valid : 1-D numpy array
        Pre-filtered daily returns (no NaNs, already sliced to window length).
    vol_target_ann : float
        Annualised vol target (e.g. 0.15).
    vol_floor : float
        Minimum realized vol; below this the scalar is clamped to 1.0 to
        prevent runaway leverage in low-vol regimes.

    Returns
    -------
    (scalar, realized_daily_vol)
        scalar             — vol scaling factor
        realized_daily_vol — realized daily std (nan if fewer than 2 obs)
    """
    if len(valid) < 2:
        return 1.0, float("nan")
    target_daily = vol_target_ann / math.sqrt(252.0)
    realized = float(np.std(valid, ddof=1))
    if realized <= vol_floor:
        return 1.0, realized
    return target_daily / realized, realized


def compute_vol_scalar(
    hist_returns: np.ndarray,
    vol_window: int,
    vol_target_ann: float,
    vol_min_obs: int | None = None,
    vol_floor: float = 1e-8,
) -> tuple[float, float]:
    """
    Wrap _scalar_from_valid with the full-history slicing pattern used by
    mtm.py, rv_zscore.py, and statistical_arbitrage.py backtest engines.

    Parameters
    ----------
    hist_returns : 1-D array-like
        All daily returns through t-1 (the [:t] slice in the backtest loop).
    vol_window : int
        Rolling window length.  0 = disabled → returns (1.0, nan).
    vol_target_ann : float
        Annualised vol target.
    vol_min_obs : int or None
        Minimum observations before scaling activates.  Defaults to vol_window.
    vol_floor : float
        Passed through to _scalar_from_valid.

    Returns
    -------
    (scalar, realized_daily_vol)
    """
    if vol_window == 0:
        return 1.0, float("nan")

    if vol_min_obs is None:
        vol_min_obs = vol_window

    arr = np.asarray(hist_returns, dtype=float)
    valid = arr[~np.isnan(arr)]

    if len(valid) < vol_min_obs:
        return 1.0, float("nan")

    return _scalar_from_valid(valid[-vol_window:], vol_target_ann, vol_floor)


# ── shared capital base ───────────────────────────────────────────────────────

# The single risk-sizing denominator for EVERY strategy and pair: the $1M
# research book (in normalized $/bbl space) that all backtests run at
# (data.lab._CAPITAL).  Per-entry capital_base values are deliberately gone:
# they were an arbitrary per-pair scaling of the same risk target (the old
# per-pair calibrate_capital() machinery is removed with them) and made lot
# counts incomparable across pairs without changing any pair's actual risk.
CAPITAL_BASE = 1_000_000.0


# ── single-leg daily sizing ───────────────────────────────────────────────────

def get_todays_size(
    signal: float,
    capital_base: float,
    ref_price: float,
    contract_multiplier: float,
    vol_scalar: float,
    realized_vol_daily: float,
    *,
    leg_fraction: float = 1.0,
    lot_rounding: float = 1.0,
    hist_dollar_pnl: np.ndarray | None = None,
    sizing_mode: str = "gaussian",
    as_of_date: str = "",
) -> dict:
    """
    Compute today's position size for a single-leg strategy (Momentum, Carry).

    Uses the vol scalar already read from the backtest result's last row, so
    the scalar and signal are numerically identical to the backtest's.  With
    the shared CAPITAL_BASE ($1M = the backtest capital) the lot count is on
    the backtested position's scale; a larger live allocation scales lots
    linearly.  The formula is the same as measures.py / mtm.py:

        lots_raw = leg_fraction × vol_scalar × |signal| × capital / (price × mult)
        lots     = round(lots_raw / lot_rounding) × lot_rounding

    Parameters
    ----------
    signal : float
        Today's signal: +1 (long), -1 (short), 0 (flat).
        Pull from result["position"].iloc[-1] — already lagged in backtest.
    capital_base : float
        The shared CAPITAL_BASE ($1M risk-sizing denominator) unless a
        caller deliberately overrides it for a larger live book.
    ref_price : float
        Today's live reference price for the contract.
    contract_multiplier : float
        $/unit per contract (e.g. 1000 bbl).
    vol_scalar : float
        Pre-computed from result["mtm"]["vol_scalar"].iloc[-1].
    realized_vol_daily : float
        result["mtm"]["realized_vol_ann"].iloc[-1] / sqrt(252).
    leg_fraction : float
        1.0 for single-leg; 0.5 when called per-leg for pairs.
    lot_rounding : float
        Round lots to this increment (1.0 = whole lots).
    hist_dollar_pnl : array or None
        Rolling dollar P&L history for sizing_mode="empirical_var".
    sizing_mode : str
        "gaussian"      — VaR = notional × realized_vol_daily × 1.645 (default)
        "empirical_var" — VaR = abs(5th percentile of hist_dollar_pnl)

    Returns
    -------
    dict: signal, direction, lots, scalar, realized_vol_ann_pct,
          notional_usd, var_95_usd, ref_price, capital_base,
          as_of_date, sizing_mode
    """
    lots_raw = (
        leg_fraction * vol_scalar * abs(signal) * capital_base
        / (ref_price * contract_multiplier)
    )

    if lot_rounding > 0:
        lots = round(lots_raw / lot_rounding) * lot_rounding
    else:
        lots = lots_raw

    if signal > 0:
        direction = "Long"
    elif signal < 0:
        direction = "Short"
    else:
        direction = "Flat"
        lots = 0.0

    notional = abs(lots) * ref_price * contract_multiplier

    if sizing_mode == "empirical_var":
        scale = abs(lots) / max(abs(lots_raw), 1e-10)
        var_95 = _empirical_var(hist_dollar_pnl, scale)
    else:
        if not math.isnan(realized_vol_daily) and realized_vol_daily > 0:
            var_95 = notional * realized_vol_daily * 1.645
        else:
            var_95 = float("nan")

    rv_ann_pct = (
        realized_vol_daily * math.sqrt(252.0) * 100.0
        if not math.isnan(realized_vol_daily)
        else float("nan")
    )

    return {
        "signal":               signal,
        "direction":            direction,
        "lots":                 lots,
        "scalar":               vol_scalar,
        "realized_vol_ann_pct": round(rv_ann_pct, 2) if not math.isnan(rv_ann_pct) else None,
        "notional_usd":         round(notional),
        "var_95_usd":           round(var_95) if not math.isnan(var_95) else None,
        "ref_price":            ref_price,
        "capital_base":         capital_base,
        "as_of_date":           as_of_date,
        "sizing_mode":          sizing_mode,
    }


# ── two-leg daily sizing ──────────────────────────────────────────────────────

def get_todays_size_pair(
    signal: float,
    capital_base: float,
    leg1_price: float,
    leg2_price: float,
    leg1_mult: float,
    leg2_mult: float,
    leg1_name: str,
    leg2_name: str,
    vol_scalar: float,
    realized_vol_daily: float,
    *,
    lot_rounding: float = 1.0,
    hist_dollar_pnl: np.ndarray | None = None,
    sizing_mode: str = "gaussian",
    as_of_date: str = "",
) -> dict:
    """
    Compute today's position size for a two-leg spread strategy (RV or Stat-Arb).

    Each leg is independently sized to 0.5 × capital, so lot counts differ
    when leg prices differ.  That is correct: dollar-neutral, not lot-neutral.
    This matches rv_zscore.py and statistical_arbitrage.py exactly:

        lots1_raw =  0.5 × vol_scalar × |signal| × capital / (leg1_price × leg1_mult)
        lots2_raw = -0.5 × vol_scalar × |signal| × capital / (leg2_price × leg2_mult)

    Parameters
    ----------
    signal : float
        +1 = long spread (long leg1, short leg2).  -1 = short.  0 = flat.
    capital_base : float
        Total capital across both legs.
    leg1_price, leg2_price : float
        Today's reference prices.
    leg1_mult, leg2_mult : float
        Contract multipliers.
    leg1_name, leg2_name : str
        Display names (e.g. "WTI", "Brent").
    vol_scalar : float
        Pre-computed from result["mtm"]["vol_scalar"].iloc[-1].
    realized_vol_daily : float
        result["mtm"]["realized_vol_ann"].iloc[-1] / sqrt(252).
    lot_rounding : float
        Each leg rounded independently.
    hist_dollar_pnl : array or None
        Combined pair dollar P&L for empirical_var mode.
    sizing_mode : str
        "gaussian" or "empirical_var".

    Returns
    -------
    dict: signal, direction, as_of_date, sizing_mode, scalar,
          realized_vol_ann_pct, legs {leg1: {...}, leg2: {...}},
          total_notional_usd, total_var_95_usd, capital_base
    """
    lots1_raw =  0.5 * vol_scalar * abs(signal) * capital_base / (leg1_price * leg1_mult)
    lots2_raw =  0.5 * vol_scalar * abs(signal) * capital_base / (leg2_price * leg2_mult)

    if signal > 0:
        direction = "Long spread"
        leg1_dir, leg2_dir = "Long", "Short"
    elif signal < 0:
        direction = "Short spread"
        leg1_dir, leg2_dir = "Short", "Long"
    else:
        direction = "Flat"
        lots1_raw = lots2_raw = 0.0
        leg1_dir = leg2_dir = "Flat"

    def _round(x: float) -> float:
        if lot_rounding > 0:
            return round(x / lot_rounding) * lot_rounding
        return x

    lots1 = _round(lots1_raw)
    lots2 = _round(lots2_raw)

    notional1 = lots1 * leg1_price * leg1_mult
    notional2 = lots2 * leg2_price * leg2_mult
    total_notional = notional1 + notional2

    if sizing_mode == "empirical_var":
        total_var = _empirical_var(hist_dollar_pnl, 1.0)
        if not math.isnan(total_var) and total_notional > 0:
            var1 = total_var * notional1 / total_notional
            var2 = total_var * notional2 / total_notional
        else:
            var1 = var2 = float("nan")
    else:
        if not math.isnan(realized_vol_daily) and realized_vol_daily > 0:
            var1 = notional1 * realized_vol_daily * 1.645
            var2 = notional2 * realized_vol_daily * 1.645
        else:
            var1 = var2 = float("nan")
        total_var = (var1 + var2) if not math.isnan(var1) else float("nan")

    rv_ann_pct = (
        realized_vol_daily * math.sqrt(252.0) * 100.0
        if not math.isnan(realized_vol_daily)
        else float("nan")
    )

    def _clean(v: float):
        return round(v) if not math.isnan(v) else None

    return {
        "signal":               signal,
        "direction":            direction,
        "as_of_date":           as_of_date,
        "sizing_mode":          sizing_mode,
        "scalar":               vol_scalar,
        "realized_vol_ann_pct": round(rv_ann_pct, 2) if not math.isnan(rv_ann_pct) else None,
        "legs": {
            leg1_name: {
                "lots":         lots1,
                "direction":    leg1_dir,
                "notional_usd": _clean(notional1),
                "var_95_usd":   _clean(var1),
                "ref_price":    leg1_price,
            },
            leg2_name: {
                "lots":         lots2,
                "direction":    leg2_dir,
                "notional_usd": _clean(notional2),
                "var_95_usd":   _clean(var2),
                "ref_price":    leg2_price,
            },
        },
        "total_notional_usd": _clean(total_notional),
        "total_var_95_usd":   _clean(total_var),
        "capital_base":       capital_base,
    }


# ── empirical VaR helper ──────────────────────────────────────────────────────

def _empirical_var(
    hist_dollar_pnl: np.ndarray | None,
    scale_factor: float = 1.0,
) -> float:
    """
    Empirical 1-day 95% VaR from the realized P&L distribution.

    Returns abs(5th percentile of hist_dollar_pnl × scale_factor).
    Caller must pass the same vol_window slice of P&L history that fed
    the vol scalar — mixing windows invalidates the comparison.

    Judgment call: requires ≥20 obs; returns nan otherwise so the caller
    can fall back gracefully rather than silently producing a thin-tail estimate.
    """
    if hist_dollar_pnl is None or len(hist_dollar_pnl) < 20:
        return float("nan")
    arr = np.asarray(hist_dollar_pnl, dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 20:
        return float("nan")
    return abs(float(np.percentile(arr, 5)) * scale_factor)


# ── default sizing configs ────────────────────────────────────────────────────
#
# capital_base is IDENTICAL everywhere by design: it is a risk-sizing
# denominator, not an allocation, so a per-pair value would only rescale lot
# counts without changing risk (the vol scalar already targets the risk
# level).  Every entry references the shared CAPITAL_BASE so per-pair drift
# cannot be reintroduced by editing one entry.
#
# UNITS: every ref_price is in the NORMALIZED $/bbl (WTI-barrel-equivalent)
# price space that load_prices produces — the same space the backtests run
# in. Unit conversion (gal->bbl etc.) happens once, in prices, via
# CONTRACT_SPECS["normalization"]; a conversion factor must never appear in
# capital_base or a ref_price on top of that. ref_price values are manual
# fallbacks — REVIEW when live prices drift materially.

SIZING_CONFIGS: dict[str, dict] = {
    "Momentum": {
        "WTI": {
            "capital_base":        CAPITAL_BASE,
            "ref_price":           80.0,    # $/bbl — REVIEW MANUALLY
            "contract_multiplier": 1_000,
            "leg_fraction":        1.0,
            "lot_rounding":        1.0,
            "vol_window":          120,
            "vol_target_ann":      0.15,
        },
        "Brent": {
            "capital_base":        CAPITAL_BASE,
            "ref_price":           82.0,    # $/bbl — REVIEW MANUALLY
            "contract_multiplier": 1_000,
            "leg_fraction":        1.0,
            "lot_rounding":        1.0,
            "vol_window":          120,
            "vol_target_ann":      0.15,
        },
        "ULSD": {
            # Normalized $/bbl (Bloomberg ¢/gal × 0.42); 1000 bbl/contract
            # (= 42,000 gal — the real HO lot).
            "capital_base":        CAPITAL_BASE,
            "ref_price":           117.6,   # $/bbl — REVIEW MANUALLY
            "contract_multiplier": 1_000,
            "leg_fraction":        1.0,
            "lot_rounding":        1.0,
            "vol_window":          120,
            "vol_target_ann":      0.15,
        },
        "RBOB": {
            # Normalized $/bbl (Bloomberg ¢/gal × 0.42); 1000 bbl/contract
            # (= 42,000 gal — the real XB lot).
            "capital_base":        CAPITAL_BASE,
            "ref_price":           113.4,   # $/bbl — REVIEW MANUALLY
            "contract_multiplier": 1_000,
            "leg_fraction":        1.0,
            "lot_rounding":        1.0,
            "vol_window":          120,
            "vol_target_ann":      0.15,
        },
    },
    "Carry": {
        "WTI": {
            "capital_base":        CAPITAL_BASE,
            "ref_price":           80.0,    # $/bbl — REVIEW MANUALLY
            "contract_multiplier": 1_000,
            "leg_fraction":        1.0,
            "lot_rounding":        1.0,
            "vol_window":          120,
            "vol_target_ann":      0.15,
        },
        "Brent": {
            "capital_base":        CAPITAL_BASE,
            "ref_price":           82.0,    # $/bbl — REVIEW MANUALLY
            "contract_multiplier": 1_000,
            "leg_fraction":        1.0,
            "lot_rounding":        1.0,
            "vol_window":          120,
            "vol_target_ann":      0.15,
        },
    },
    # Pair strategies: capital_base covers BOTH legs combined; each leg is
    # independently sized to 0.5 × capital at its own price (dollar-neutral).
    "Stat-Arb": {
        "WTI / Brent": {
            "capital_base":        CAPITAL_BASE,
            "leg1_ref_price":      80.0,    # WTI $/bbl — REVIEW MANUALLY
            "leg2_ref_price":      82.0,    # Brent $/bbl — REVIEW MANUALLY
            "leg1_multiplier":     1_000,
            "leg2_multiplier":     1_000,
            "leg_fraction":        0.5,
            "lot_rounding":        1.0,
            "vol_window":          120,
            "vol_target_ann":      0.15,
            "anchor_leg":          "WTI",
        },
        "Brent / RBOB": {
            "capital_base":        CAPITAL_BASE,
            "leg1_ref_price":      82.0,    # Brent $/bbl — REVIEW MANUALLY
            "leg2_ref_price":      113.4,   # RBOB $/bbl normalized — REVIEW MANUALLY
            "leg1_multiplier":     1_000,
            "leg2_multiplier":     1_000,
            "leg_fraction":        0.5,
            "lot_rounding":        1.0,
            "vol_window":          120,
            "vol_target_ann":      0.15,
            "anchor_leg":          "Brent",
        },
        "ULSD / WTI": {
            "capital_base":        CAPITAL_BASE,
            "leg1_ref_price":      117.6,   # ULSD $/bbl normalized — REVIEW MANUALLY
            "leg2_ref_price":      80.0,    # WTI $/bbl — REVIEW MANUALLY
            "leg1_multiplier":     1_000,
            "leg2_multiplier":     1_000,
            "leg_fraction":        0.5,
            "lot_rounding":        1.0,
            "vol_window":          120,
            "vol_target_ann":      0.15,
            "anchor_leg":          "WTI",
        },
    },
    "RV": {
        "WTI / Brent": {
            "capital_base":        CAPITAL_BASE,
            "leg1_ref_price":      80.0,    # WTI $/bbl — REVIEW MANUALLY
            "leg2_ref_price":      82.0,    # Brent $/bbl — REVIEW MANUALLY
            "leg1_multiplier":     1_000,
            "leg2_multiplier":     1_000,
            "leg_fraction":        0.5,
            "lot_rounding":        1.0,
            "vol_window":          120,
            "vol_target_ann":      0.15,
            "anchor_leg":          "WTI",
        },
    },
}

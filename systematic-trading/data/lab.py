"""
data/lab.py — Strategy Lab compute layer.

Parameterized strategy runs against the in-memory price store (data.loader)
for all four strategy families:

    Momentum  — MA-crossover speed tiers (or custom MA pair) via energy.strategies.momentum
    Carry     — F(front) − F(end) curve-slope signal via energy.strategies.carry
    Stat-Arb  — z-score mean reversion on product spreads via rv_zscore / rv_regression
    COT       — Managed Money positioning signals (synthetic data until cot_bbg lands)

Architecture notes
------------------
* run_lab(params) computes a result and returns a short cache KEY. The Dash
  compute callback stores only the key in a dcc.Store; display callbacks call
  get_result(key) to pull the full result from this module-level LRU cache.
  Results are pandas-heavy and must not round-trip through the browser.
* The key embeds the normalized params as JSON, so an evicted (or
  post-restart) key can always be recomputed transparently.
* Engine math runs on loader.get_prices_normalized() (the $/bbl-equivalent
  basis the strategies were calibrated on); "price space" outputs for
  directional strategies are converted back to native quote units for display.
* Parameter sweeps report PRICE-SPACE Sharpe (unsized signal PnL): it is the
  cleaner anti-overfitting view (no vol-targeting distortion) and is fast
  enough to compute over a full grid. Legs/roll paths are prepared once per
  sweep, not once per grid cell.
* EXECUTION TIMING (standardized 2026-07-09, sole convention — the old
  Same-day/T-1 lag toggle is removed): direction for date t is fixed at
  t-1's close; the position is established at t's settle; its first P&L
  accrues over t -> t+1. The MTM capital loops implement this natively
  (contracts[t-1] earn flow[t]); every price-space/sweep layer books the
  per-unit twin signal[t-1] * flow[t] so both layers share one methodology.
"""
import json
import warnings
from collections import OrderedDict

import numpy as np
import pandas as pd

from energy.accounting.booking import held_pnl

_TRADE_START = "2015-01-01"
_VOL_WINDOW = 120
_CAPITAL = 1_000_000
UKRAINE_SPLIT = "2022-02-24"   # pre/post invasion sub-period boundary

STRATEGIES = ["Momentum", "Carry", "Stat-Arb", "COT"]

# Speed tiers from the strategy doc — each blends 3 MA pairs equal-weight.
MOMENTUM_TIERS: dict[str, list[tuple[int, int]]] = {
    "Very Fast": [(1, 5), (2, 10), (3, 15)],
    "Fast":      [(1, 5), (5, 20), (10, 60)],
    "Medium":    [(10, 30), (20, 60), (30, 90)],
    "Slow":      [(20, 120), (40, 180), (60, 250)],
}
MOMENTUM_TIERS["Averaged"] = [p for tier in ("Very Fast", "Fast", "Medium", "Slow")
                              for p in MOMENTUM_TIERS[tier]]

STAT_ARB_PAIRS: list[tuple[str, str]] = [
    ("WTI", "Brent"),
    ("Brent", "RBOB"),
    ("Brent", "ULSD"),
    ("ULSD", "WTI"),
    ("Ethane", "Natgas"),
    ("Propane", "Ethane"),
    ("Propane", "Butane"),
    ("RBOB", "Butane"),
]

# Per-pair default-view config (lookback, entry, month_offset, cross_arb).
# Canonical source: energy.analytics.signal_summary — the SAME
# object the Signals page cards and Levels panel read, so the lab's default
# runs cannot diverge from what those pages display.
from energy.analytics.signal_summary import STAT_ARB_PAIR_DEFAULTS, pair_defaults

# Roll tenor -> CONTRACT_SPECS roll-config key. For strict pairs the
# coordinated engine maps these to delivery-month leads of 0/3/6/12 on the
# prompt-anchored roll cadence (spread_rolling.ROLL_CONFIG_LEAD_MONTHS);
# rank_approximate pairs use the F-column configs of the same names.
# Deferred tenors trade less and carried less edge in research — exposed as
# an exploration control, not a recommendation.
ROLL_TENORS = {
    "Prompt": "prompt_EOM_roll",
    "Q2": "q2_deferred_roll",
    "Q3": "q3_deferred_roll",
    "1yr": "1yr_deferred_roll",
}
# Legacy label from the two-option selector (pre-2026-07-16) — old cache
# keys / saved states must keep resolving.
_ROLL_TENOR_ALIASES = {"Deferred (1Y)": "1yr"}

COT_SIGNALS = ["Follow the Flow", "Fade the Crowd"]

# Sweep grids (fixed — results cached per commodity/pair).
MOMENTUM_SWEEP_FAST = [1, 2, 3, 5, 10, 20, 30, 40, 60]
MOMENTUM_SWEEP_SLOW = [5, 10, 15, 20, 30, 60, 90, 120, 180, 250]
STAT_ARB_SWEEP_LOOKBACKS = [10, 20, 40, 60, 90, 120, 180, 250]
STAT_ARB_SWEEP_THRESHOLDS = [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0]
# Dollar-band mode ($/bbl entry thresholds — Bouchouev/Zuo convention,
# RESEARCH config until the regime test clears it)
STAT_ARB_SWEEP_DOLLAR_THRESHOLDS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5]


# ---------------------------------------------------------------------------
# Module-level result caches (server-side; only keys travel through dcc.Store)
# ---------------------------------------------------------------------------
_RESULTS: "OrderedDict[str, dict]" = OrderedDict()
_SWEEPS: "OrderedDict[str, pd.DataFrame]" = OrderedDict()
_MAX_RESULTS = 24
_MAX_SWEEPS = 16


def _cache_put(cache: OrderedDict, max_size: int, key: str, value) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > max_size:
        cache.popitem(last=False)


def clear_caches() -> int:
    """
    Drop every cached result and sweep. Returns how many entries were evicted.

    MUST be called whenever data.loader re-pulls prices. Cache keys are the
    normalized params alone (see run_lab) with no data-generation component, so
    a refreshed price store does NOT invalidate them — without this, every
    subsequent get_result() serves a backtest computed on the previous pull.
    The signal/levels caches key off loader._loaded_at and self-invalidate; this
    one cannot, because its keys are what the browser round-trips.
    """
    n = len(_RESULTS) + len(_SWEEPS)
    _RESULTS.clear()
    _SWEEPS.clear()
    return n


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _specs() -> dict:
    from energy.accounting.contract_specs import CONTRACT_SPECS
    return CONTRACT_SPECS


def available_commodities() -> list[str]:
    """Commodities with both loaded prices and a usable roll config."""
    from data import loader

    specs = _specs()
    return sorted(
        name for name in loader.loaded_commodities()
        if specs.get(name, {}).get("prompt_EOM_roll")
    )


def available_fcols(commodity: str) -> list[str]:
    """F-columns with at least one non-NaN value, ordered F1..F24."""
    from data import loader

    df = loader.get_prices(commodity)
    cols = [c for c in df.columns if df[c].notna().any()]
    return sorted(cols, key=lambda x: int(x[1:]))


def pair_label(leg1: str, leg2: str) -> str:
    return f"{leg1} / {leg2}"


def _pair_from_label(label: str) -> tuple[str, str]:
    leg1, leg2 = (s.strip() for s in label.split("/"))
    return leg1, leg2


def _months_or_none(commodity: str):
    """CONTRACT_MONTH_YR frame for the coordinated spread construction.
    None if unavailable — prepare_spread_legs then raises for
    strict_delivery_match pairs (by design: no silent fallback to the
    disbanded uncoordinated construction)."""
    from data import loader
    try:
        return loader.get_contract_months(commodity)
    except (KeyError, RuntimeError):
        return None


def _bloomberg_check(leg1: str, leg2: str, leg1_df: pd.DataFrame, leg2_df: pd.DataFrame,
                      spread_meta: dict) -> dict | None:
    """
    Standing sanity check: for pairs with a SPREAD_SPECS validate_ticker
    (e.g. WTI/Brent -> Bloomberg S:ENCO), compare this run's coordinated
    construction against the independent listed-spread series and surface
    the result on every call — a future regression (wrong leg, stitched-diff
    reintroduced, broken month lookup, unsynchronized roll) degrades the
    month-matched comparison and trips `ok=False` here without needing a
    fresh multi-hour audit. Soft-fails (returns an error dict) rather than
    breaking the run if FlowsDB or the ticker is unavailable.
    """
    if spread_meta.get("construction") != "coordinated_delivery_month":
        return None
    from energy.accounting.spread_specs import get_spread_spec
    from energy.strategies.spread_rolling import validate_against_listed_spread
    from data import loader

    spec = get_spread_spec(leg1, leg2) or {}
    ticker = spec.get("validate_ticker")
    if not ticker:
        return None
    offset = spread_meta.get("month_offset") or 0
    if offset != 0:
        # The listed spread quotes the SAME-delivery-month pair (verified for
        # S:ENCO: median |ENCO(M) - (WTI(M)-Brent(M))| = 0.0000 — Pass 8 H).
        # A month-offset construction is a different instrument, so a level
        # comparison is structurally meaningless — skip with a reason rather
        # than surface a misleading FAIL badge. The offset-0 view still runs
        # the full check (and tests/test_bloomberg_crosscheck.py gates it).
        return {"skipped": f"{ticker} quotes the same-delivery-month spread; "
                           f"it cannot validate a month_offset={offset:+d} "
                           "construction"}
    try:
        listed_price, listed_month = loader.get_listed_spread(ticker)
        return validate_against_listed_spread(
            leg1_df, leg2_df, listed_price, listed_month,
            month_offset=offset,
        )
    except Exception as exc:
        return {"error": str(exc)}


def _build_roll(commodity: str, prices: pd.DataFrame, expiry_cal) -> pd.DataFrame:
    """Build the EOM roll path for one commodity (mirrors data/signals.py wiring)."""
    from energy.accounting.mtm import build_roll_path

    cfg = _specs()[commodity]["prompt_EOM_roll"]
    roll_kwargs: dict = dict(
        prices=prices,
        expiry_calendar=expiry_cal,
        style=cfg["style"],
        front_col=cfg.get("front_col", "F1"),
        next_col=cfg.get("next_col", "F2"),
        third_col=cfg.get("third_col", "F3"),
        far_col=cfg.get("far_col", "F4"),
    )
    if cfg.get("roll_window") is not None:
        roll_kwargs["roll_window"] = cfg["roll_window"]
    if cfg.get("mid_col") is not None:
        roll_kwargs["mid_col"] = cfg["mid_col"]
    return build_roll_path(**roll_kwargs)


def _full_history_roll(commodity: str):
    """(roll_path, prices_full) on the FULL price history (expiry calendar
    unclipped) — shared by the clean signal series and the flow-based vol
    warmup so both read the same roll-aware path."""
    from data import loader

    spec = _specs()[commodity]
    prices_full = loader.get_prices_normalized(commodity)
    expiry_full = loader.get_expiry(spec["ticker"])
    return _build_roll(commodity, prices_full, expiry_full), prices_full


def _flow_warmup_returns(commodity: str) -> pd.Series | None:
    """
    Pre-trade-window daily returns for seeding vol estimators, in the SAME
    space every capital loop measures in-sample vol: roll-aware flow over
    the previous held price (daily_pnl / held_price.shift(1)).

    Never a raw generic column's pct_change: raw generics jump at every
    re-rank even when no price moved, so a stitched warmup seeds the vol
    estimator with phantom moves and then hands off to a flow-based
    in-sample stream — two different definitions inside one window (the
    sizing-layer twin of the 2026-07-14 signal-series fix).
    """
    from energy.accounting.mtm import build_held_price_series

    roll_full, prices_full = _full_history_roll(commodity)
    held_px = build_held_price_series(roll_full, prices_full)
    ret = (roll_full["daily_pnl"] / held_px.shift(1)).dropna()
    pre = ret[ret.index < pd.Timestamp(_TRADE_START)]
    return pre if len(pre) > 1 else None


def _clean_signal_prices(commodity: str) -> pd.DataFrame:
    """
    Full-history CLEAN signal price frame for single-leg strategies: the
    leg's roll-aware flow cumsum anchored at the first held price (see
    energy.strategies.rolling.leg_signal_series), as a one-column frame
    ('SIGNAL') consumable by momentum()'s prices_signal/front_col interface.

    Raw generic F-columns jump at every re-rank even when no price moved,
    which biases any trailing-window signal computed on them (momentum
    Sharpe 0.282 -> 0.125 when corrected — 2026-07-14 signal-series audit);
    signals must read this series instead. Built on the full price history
    so MAs warm up before the trade window.
    """
    from energy.strategies.rolling import leg_signal_series

    roll_full, prices_full = _full_history_roll(commodity)
    return pd.DataFrame({"SIGNAL": leg_signal_series(roll_full, prices_full)})


def _load_commodity(commodity: str):
    """(prices_norm, prices_native, prices_norm_full, expiry) clipped to the trade window."""
    from data import loader

    spec = _specs()[commodity]
    prices_full = loader.get_prices_normalized(commodity)
    prices_native_full = loader.get_prices(commodity)
    prices = prices_full[prices_full.index >= _TRADE_START].copy()
    prices_native = prices_native_full[prices_native_full.index >= _TRADE_START].copy()

    expiry_cal = loader.get_expiry(spec["ticker"])
    expiry_cal = expiry_cal[expiry_cal >= pd.Timestamp(_TRADE_START)]
    return prices, prices_native, prices_full, expiry_cal


def _directional_result(
    commodity: str,
    strategy: str,
    label: str,
    path: pd.DataFrame,
    prices: pd.DataFrame,
    prices_native: pd.DataFrame,
    vol_target: float,
    vol_window: int,
    vol_scalar_cap: float | None = None,
) -> dict:
    """Shared accounting for momentum / carry / COT (single-leg ±1 paths)."""
    from energy.accounting.mtm import build_held_price_series
    from energy.accounting.measures import build_measures
    from energy.analytics.metrics import legacy_capstone_metrics

    spec = _specs()[commodity]
    norm_scale = spec.get("normalization", 1.0) or 1.0

    held_price = build_held_price_series(path, prices)
    held_price_native = build_held_price_series(path, prices_native)

    # Vol warmup in the SAME space the loop measures in-sample vol —
    # roll-aware flow / prev held price. The old raw F-column pct_change
    # warmup carried a re-rank jump every month (phantom vol; the
    # sizing-layer twin of the 2026-07-14 signal-series fix).
    warmup = _flow_warmup_returns(commodity)

    measures = build_measures(
        daily_pnl=path["daily_pnl"],
        ref_price=held_price,
        signal=path["position"],
        rebalance_flag=path["rebalance_flag"],
        t_cost_abs=spec.get("t_cost_abs", 0.0),
        initial_capital=_CAPITAL,
        contract_multiplier=spec["contract_multiplier"],
        vol_window=vol_window,
        vol_target_ann=vol_target,
        vol_scalar_cap=vol_scalar_cap,
        warmup_returns=warmup,
    )

    # Convert engine-basis ($/bbl-equivalent) price space back to native quote units.
    price_space = measures["price_space"].copy()
    for col in ("daily_pnl", "t_cost", "net_pnl", "cum_pnl", "cum_net_pnl"):
        price_space[col] = price_space[col] / norm_scale

    return dict(
        kind="directional",
        strategy=strategy,
        commodity=commodity,
        label=label,
        held_price_native=held_price_native,
        position=path["position"],
        price_space=price_space,
        price_space_metrics=legacy_capstone_metrics(price_space, contracts=1, units=1),
        mtm=measures["mtm"],
        mtm_metrics=measures["mtm_metrics"],
        norm_scale=norm_scale,
    )


# ---------------------------------------------------------------------------
# Strategy runners
# ---------------------------------------------------------------------------

def _run_momentum(p: dict) -> dict:
    from energy.strategies.momentum import momentum

    commodity = p["commodity"]
    prices, prices_native, prices_full, expiry_cal = _load_commodity(commodity)
    roll_path = _build_roll(commodity, prices, expiry_cal)

    if p["tier"] == "Custom":
        ma_pairs = [(int(p["fast"]), int(p["slow"]))]
        label = f"{commodity} Momentum MA({p['fast']},{p['slow']})"
    else:
        ma_pairs = MOMENTUM_TIERS[p["tier"]]
        label = f"{commodity} Momentum — {p['tier']}"

    # Signal reads the CLEAN roll-aware cumsum series (full history pre-warms
    # the MAs) — never a raw generic F-column (re-rank jumps bias the MAs).
    sig_prices = _clean_signal_prices(commodity)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        path = momentum(
            prices=prices,
            rolled_df=roll_path,
            front_col="SIGNAL",
            ma_pairs=ma_pairs,
            prices_signal=sig_prices,
        )

    return _directional_result(commodity, "Momentum", label, path,
                               prices, prices_native, p["vol_target"], p["vol_window"])


def _run_carry(p: dict) -> dict:
    from energy.strategies.carry import carry

    commodity = p["commodity"]
    prices, prices_native, prices_full, expiry_cal = _load_commodity(commodity)
    roll_path = _build_roll(commodity, prices, expiry_cal)

    eps_pct = float(p.get("epsilon", 0.0))
    path = carry(
        prices=prices,
        rolled_df=roll_path,
        front_col=p["carry_front"],
        end_col=p["carry_end"],
        epsilon_pct=eps_pct,
    )
    eps_label = f" buf={eps_pct:.0f}%" if eps_pct > 0 else ""
    label = f"{commodity} Carry {p['carry_front']}−{p['carry_end']}{eps_label}"
    return _directional_result(commodity, "Carry", label, path,
                               prices, prices_native, p["vol_target"], p["vol_window"])


def _run_stat_arb(p: dict) -> dict:
    """
    Z-score pair stat-arb.  Signal from rv_zscore._build_signal; MTM from
    the dollar-neutral stat-arb capital loop (each leg independently sized
    to 0.5 x capital at its own price — the book's confirmed sizing
    convention, matching the /api/sizing pair panel; replaced the
    equal-lots-at-average-price build_measures spread mode on 2026-07-08).

    hedge="ols" still uses rv_regression's internal capital loop.
    """
    from energy.strategies.relative_value.rv_zscore import (
        _build_spread, _build_signal, _build_pair_pnl,
    )
    from energy.strategies.spread_rolling import (
        prepare_spread_legs, spread_level_series, spread_signal_series,
    )
    from energy.strategies.rolling import spread_pnl_from_legs
    from energy.strategies.relative_value.rv_regression import rv_regression
    from energy.strategies.relative_value.statistical_arbitrage import (
        _stat_arb_capital_loop,
    )
    from energy.analytics.metrics import legacy_capstone_metrics, metrics as mtm_metrics_fn
    from energy.accounting.mtm import build_held_price_series
    from data import loader

    leg1, leg2 = _pair_from_label(p["pair"])
    specs = _specs()
    prices1 = loader.get_prices_normalized(leg1)
    prices2 = loader.get_prices_normalized(leg2)
    expiry1 = loader.get_expiry(specs[leg1]["ticker"])
    expiry2 = loader.get_expiry(specs[leg2]["ticker"])
    roll_tenor = _ROLL_TENOR_ALIASES.get(p["roll_tenor"], p["roll_tenor"])
    roll_config = ROLL_TENORS[roll_tenor]

    lookback = int(p["lookback"])
    entry = float(p["entry"])
    # Exit rule is mean-cross only (2026-07-16): flat when the deviation
    # crosses zero. The match-entry variant was removed from the UI/config
    # path; the engine's exit_threshold parameter remains for research use.
    exit_thr = 0.0
    # Entry-band units: "zscore" (σ, production default) or "dollar" ($/bbl
    # deviation from the rolling mean — Bouchouev/Zuo convention). RESEARCH
    # config: same clean signal series, same exit state machine, only the
    # units of the entry/exit comparison change (regime test 2026-07-15).
    band_mode = p.get("band_mode") or "zscore"
    mult = float(specs[leg1].get("contract_multiplier", 1000))
    mult2 = float(specs[leg2].get("contract_multiplier", 1000))

    if p["hedge"] == "ols":
        if band_mode == "dollar":
            raise ValueError(
                "band_mode='dollar' is not available with hedge='ols' — the OLS "
                "residual is not in $/bbl spread units; use hedge='50/50'.")
        pack = rv_regression(
            legs=[leg1, leg2],
            prices_list=[prices1, prices2],
            expiry_list=[expiry1, expiry2],
            initial_capital=_CAPITAL,
            lookback=lookback,
            zscore_threshold=entry,
            vol_window=0,  # stat-arb sizing is identity (vol scalar ≡ 1.0, 2026-07-16)
            vol_target_ann=p["vol_target"],
            trade_start=_TRADE_START,
            roll_config=roll_config,
        )
        sdf = pack["strategy_df"]
        spread_df = pd.DataFrame({
            "spread": sdf["residual"],
            "spread_mean": sdf["resid_mean"],
            "upper_band": sdf["upper_band"],
            "lower_band": sdf["lower_band"],
            "zscore": sdf["zscore"],
        })
        # Price-space P&L from leg-level roll-aware flows scaled by prior-day
        # OLS weights — NEVER a diff of the residual level: the residual is
        # built on stitched held prices that jump at every roll/relabel, and
        # those jumps are not tradable P&L (same defect class as the
        # stitched-spread diff fixed in the 50/50 branch below). The residual
        # level stays for charting only. held_pnl books signal[t-1] and
        # weights[t-1] against flow[t] — the capital loop's timing.
        leg_flows = pd.DataFrame({
            name: df["daily_pnl"].reindex(sdf.index).fillna(0.0)
            for name, df in zip([leg1, leg2], pack["leg_dfs"])
        })
        unit_pnl = held_pnl(
            sdf["signal_raw"], leg_flows, weights=pack["weights_df"][[leg1, leg2]],
            zero_first=False, fill_value=0.0,
        )
        price_space = pd.DataFrame(index=sdf.index)
        price_space["daily_pnl"] = unit_pnl
        price_space["t_cost"] = 0.0
        price_space["net_pnl"] = unit_pnl
        price_space["cum_pnl"] = unit_pnl.cumsum()
        price_space["cum_net_pnl"] = price_space["cum_pnl"]
        return dict(
            kind="pair", strategy="Stat-Arb", commodity=p["pair"],
            label=f"{pair_label(leg1, leg2)} — z {entry:.2f}σ / lb {lookback} / ols",
            leg1=leg1, leg2=leg2,
            position=sdf["signal_raw"], spread=spread_df,
            entry_threshold=entry, exit_threshold=None,
            price_space=price_space,
            price_space_metrics=legacy_capstone_metrics(price_space, contracts=1, units=1),
            mtm=sdf, mtm_metrics=mtm_metrics_fn(sdf),
            # rv_regression still builds its own per-leg rank paths internally
            spread_construction=None,
        )

    # ── 50/50 z-score via build_measures ──────────────────────────────────
    # Coordinated delivery-month construction for strict pairs (rank path
    # for rank_approximate pairs) — see energy.accounting.spread_specs.
    leg1_df, leg2_df, spread_meta = prepare_spread_legs(
        leg1, leg2, prices1, prices2, expiry1, expiry2,
        months1=_months_or_none(leg1), months2=_months_or_none(leg2),
        roll_config=roll_config,
        roll_trigger_style=p.get("roll_trigger_style"),
        spread_style=p.get("spread_style"),
        month_offset=p.get("month_offset"),
    )
    spread_meta["bloomberg_check"] = _bloomberg_check(leg1, leg2, leg1_df, leg2_df, spread_meta)

    # Signal on full history: rolling window warm from 2010, state machine
    # carries through so positions at trade start reflect prior regime.
    # The signal reads spread_signal_series — the CLEAN flow-cumsum series
    # (shared with the sweep and the Signals/Levels cards). The stitched
    # level (spread_level_series) is kept for quoted-level charting only:
    # its relabel jumps biased the traded z +0.38 sigma (Pass 8 fix,
    # notes/pass8_signal_series_integrity_2026-07-14.md).
    spread_signal_full = spread_signal_series(leg1_df, leg2_df)
    spread_quoted_full = spread_level_series(leg1_df, leg2_df)
    idx_full = spread_signal_full.index
    signal_result = _build_signal(spread_signal_full, lookback, entry, exit_thr,
                                  band_mode=band_mode)

    idx = idx_full[idx_full >= pd.Timestamp(_TRADE_START)]
    signal = signal_result["signal_raw"].reindex(idx).fillna(0.0)

    spread_series = spread_signal_full.reindex(idx)
    # P&L from leg-level roll-aware flows (never a stitched-level diff — that
    # books every roll/relabel jump on either leg as phantom, non-tradable P&L).
    leg_flow_pnl = spread_pnl_from_legs(leg1_df, leg2_df)
    spread_pnl = leg_flow_pnl["daily_pnl"].reindex(idx).fillna(0.0)
    leg1_pnl = leg_flow_pnl["leg1_daily_pnl"].reindex(idx).fillna(0.0)
    leg2_pnl = leg_flow_pnl["leg2_daily_pnl"].reindex(idx).fillna(0.0)
    roll_flag = leg_flow_pnl["roll_day_flag"].reindex(idx).fillna(0)
    rp1 = leg1_df["held_price"].reindex(idx)
    rp2 = leg2_df["held_price"].reindex(idx)

    # Price space: signed leg-flow spread P&L, no capital sizing, booked via
    # the sanctioned primitive (signal[t-1] * flow[t] — the capital loop's
    # timing; see energy.accounting.booking).
    price_space = pd.DataFrame(index=idx)
    price_space["daily_pnl"] = held_pnl(signal, spread_pnl, zero_first=False).astype(float)
    price_space["t_cost"] = 0.0
    price_space["net_pnl"] = price_space["daily_pnl"]
    price_space["cum_pnl"] = price_space["daily_pnl"].cumsum()
    price_space["cum_net_pnl"] = price_space["cum_pnl"]

    # MTM: dollar-neutral per leg — each leg independently sized to
    # 0.5 x capital at its own price at every rebalance (signal change or
    # live roll), fixed contracts between.  Same validated engine the pair
    # sizing panel documents matching (statistical_arbitrage.py).
    # SIZING IS IDENTITY for stat-arb (2026-07-16): vol_window=0 forces the
    # vol scalar to a constant 1.0 — no vol targeting, no cap, no warmup.
    # Four sizing schemes (aggressive |z|, capped |z|, scale-in, lb20) all
    # lost to constant size in research; the capped scalar was itself
    # ≈constant (3.86x) and added complexity without risk-adjusted benefit.
    mtm = _stat_arb_capital_loop(
        signal_raw=signal,
        leg1_pnl_price=leg1_pnl,
        leg2_pnl_price=leg2_pnl,
        leg1_held_price=rp1,
        leg2_held_price=rp2,
        roll_day_flag=roll_flag,
        mult1=mult,
        mult2=mult2,
        tc1=0.0,
        tc2=0.0,
        initial_capital=_CAPITAL,
        vol_window=0,
    )
    # Compat columns under the names build_measures used to emit, consumed
    # by /api/sizing and the detail charts. realized_vol_ann is now
    # INFORMATIONAL ONLY (VaR display in the sizing panel) — a trailing
    # 120d vol of the pair's own returns; the sizing layer never reads it.
    pair_ret_disp = (0.5 * leg1_pnl / rp1.ffill().shift(1)
                     - 0.5 * leg2_pnl / rp2.ffill().shift(1))
    mtm["realized_vol_ann"] = (
        pair_ret_disp.rolling(120, min_periods=20).std() * np.sqrt(252.0)
    )
    mtm["ref_price"] = (rp1 + rp2) / 2.0
    mtm["dollar_pnl"] = mtm["gross_pnl"]
    mtm["txn_cost_mtm"] = mtm["t_cost"]

    spread_df = pd.DataFrame(index=idx)
    for col in ["spread", "spread_mean", "upper_band", "lower_band", "deviation_pct"]:
        if col in signal_result.columns:
            spread_df[col] = signal_result[col].reindex(idx)
    spread_df.rename(columns={"deviation_pct": "zscore"}, inplace=True)
    if "spread" not in spread_df.columns:
        spread_df["spread"] = spread_series
    # trader-facing quoted level, chart overlay only — no signal math reads it
    spread_df["quoted_spread"] = spread_quoted_full.reindex(idx)

    band_label = (f"z {entry:.2f}σ" if band_mode == "zscore"
                  else f"$-band {entry:.2f}$")
    tenor_label = f" / {roll_tenor}" if roll_tenor != "Prompt" else ""
    offset_val = spread_meta.get("month_offset") or 0
    offset_label = f" / offset {offset_val:+d}" if offset_val else ""
    research = (pair_defaults(p["pair"]).get("research_only")
                or band_mode == "dollar")  # $-band is RESEARCH until cleared
    research_label = " / RESEARCH-ONLY" if research else ""
    return dict(
        kind="pair", strategy="Stat-Arb", commodity=p["pair"],
        label=f"{pair_label(leg1, leg2)} — {band_label} / lb {lookback} / mean-cross{tenor_label}{offset_label} / 50-50{research_label}",
        leg1=leg1, leg2=leg2,
        position=signal, spread=spread_df,
        entry_threshold=entry, exit_threshold=exit_thr,
        band_mode=band_mode, roll_tenor=roll_tenor,
        price_space=price_space,
        price_space_metrics=legacy_capstone_metrics(price_space, contracts=1, units=1),
        mtm=mtm,
        mtm_metrics=mtm_metrics_fn(mtm),
        spread_construction=spread_meta,
    )


def _run_cot(p: dict) -> dict:
    """
    COT positioning strategy: weekly Managed Money signal (synthetic until
    cot_bbg lands) lagged to its publication date, forward-filled onto the
    daily roll path and run through the standard dual-measure accounting.
    """
    from data import cot

    commodity = p["commodity"]
    prices, prices_native, prices_full, expiry_cal = _load_commodity(commodity)
    roll_path = _build_roll(commodity, prices, expiry_cal)

    cot_df = cot.get_cot(commodity)
    if p["cot_signal"] == "Fade the Crowd":
        sig_df = cot.fade_the_crowd(cot_df, threshold_pct=float(p["cot_threshold"]))
        label = f"{commodity} COT — Fade the Crowd ({p['cot_threshold']:.0f}/{100 - p['cot_threshold']:.0f})"
    else:
        sig_df = cot.follow_the_flow(cot_df, fast=int(p["cot_fast"]), slow=int(p["cot_slow"]))
        label = f"{commodity} COT — Follow the Flow MA({p['cot_fast']},{p['cot_slow']}w)"

    position = cot.weekly_to_daily_position(sig_df["signal"], roll_path.index)

    prev = position.shift(1).fillna(0.0)
    position_change = (position != prev).astype(int)
    live_roll = ((roll_path["roll_day_flag"] == 1) & (position != 0)).astype(int)
    rebalance = pd.Series(np.maximum(position_change, live_roll), index=roll_path.index)
    if len(rebalance):
        rebalance.iloc[0] = 1

    path = pd.DataFrame(index=roll_path.index)
    path["daily_pnl"] = roll_path["daily_pnl"]
    path["held_contract"] = roll_path["held_contract"]
    path["position"] = position
    path["rebalance_flag"] = rebalance

    result = _directional_result(commodity, "COT", label, path,
                                 prices, prices_native, p["vol_target"], p["vol_window"])
    result["cot"] = cot_df
    result["cot_signal_df"] = sig_df
    result["cot_mode"] = p["cot_signal"]
    result["cot_synthetic"] = cot.is_synthetic()
    return result


# ---------------------------------------------------------------------------
# Public entry points — params in, cache key out
# ---------------------------------------------------------------------------

_RUNNERS = {
    "Momentum": _run_momentum,
    "Carry": _run_carry,
    "Stat-Arb": _run_stat_arb,
    "COT": _run_cot,
}

DEFAULTS = dict(
    strategy="Momentum",
    commodity="WTI",
    tier="Averaged", fast=5, slow=60,
    carry_front="F4", carry_end="F15", epsilon=0.0,
    pair=pair_label(*STAT_ARB_PAIRS[0]),
    lookback=20, entry=1.5, hedge="50/50",
    band_mode="zscore",
    roll_tenor="Prompt", month_offset=0,
    cot_signal="Follow the Flow", cot_fast=4, cot_slow=16, cot_threshold=20.0,
    vol_target=0.15, vol_window=_VOL_WINDOW,
)

# Only these keys enter the cache key per strategy, so wiggling a hidden
# control from another strategy's panel never triggers a recompute.
_RELEVANT_KEYS = {
    "Momentum": ["commodity", "tier", "fast", "slow"],
    "Carry": ["commodity", "carry_front", "carry_end", "epsilon"],
    # exit/exit_mode removed 2026-07-16 (mean-cross is the sole exit rule);
    # vol_scalar_cap removed same day (stat-arb sizing is identity).
    "Stat-Arb": ["pair", "lookback", "entry", "hedge", "roll_tenor", "month_offset", "band_mode"],
    "COT": ["commodity", "cot_signal", "cot_fast", "cot_slow", "cot_threshold"],
}


def normalize_params(raw: dict) -> dict:
    """Fill defaults and strip params irrelevant to the selected strategy."""
    merged = {**DEFAULTS, **{k: v for k, v in raw.items() if v is not None}}
    strategy = merged["strategy"] if merged["strategy"] in STRATEGIES else "Momentum"
    # Per-pair stat-arb defaults from the shared default-view config
    # (signal_summary.pair_defaults) — any field the caller didn't set
    # resolves to the same values the Signals/Levels cards display.
    if strategy == "Stat-Arb":
        cfg = pair_defaults(merged["pair"])
        # Legacy roll-tenor label from the old two-option selector.
        merged["roll_tenor"] = _ROLL_TENOR_ALIASES.get(
            merged["roll_tenor"], merged["roll_tenor"])
        if raw.get("lookback") is None:
            merged["lookback"] = cfg["lookback"]
        if raw.get("entry") is None:
            # Pair defaults are calibrated in σ. In dollar mode an unset
            # entry falls back to $1.00 (the paper's canonical threshold),
            # never the σ default reinterpreted as dollars.
            merged["entry"] = (1.0 if merged.get("band_mode") == "dollar"
                               else cfg["entry"])
        if raw.get("month_offset") is None:
            merged["month_offset"] = cfg["month_offset"]
        # Sizing is identity for stat-arb (vol scalar ≡ 1.0): pin the vol
        # fields so cache keys are stable regardless of caller-sent values,
        # and the runner's vol_window=0 path is the only one that exists.
        merged["vol_target"] = 0.15
        merged["vol_window"] = 0
    out = {"strategy": strategy,
           "vol_target": float(merged["vol_target"]),
           "vol_window": int(merged["vol_window"])}
    for k in _RELEVANT_KEYS[strategy]:
        out[k] = merged[k]
    if strategy == "Momentum" and out.get("tier") != "Custom":
        out.pop("fast", None)
        out.pop("slow", None)
        out.setdefault("tier", "Averaged")
    return out


def run_lab(raw_params: dict) -> str:
    """Compute (or fetch cached) lab result; returns the cache key."""
    params = normalize_params(raw_params)
    key = json.dumps(params, sort_keys=True)
    if key not in _RESULTS:
        # Re-inject defaults for runner convenience (normalize stripped them)
        full = {**DEFAULTS, **params}
        _cache_put(_RESULTS, _MAX_RESULTS, key, _RUNNERS[params["strategy"]](full))
    else:
        _RESULTS.move_to_end(key)
    return key


def get_result(key: str) -> dict:
    """Fetch a result by key; transparently recomputes if evicted/restarted.

    A key minted before a normalization change (e.g. the 2026-07-16 removal
    of exit/exit_mode/vol_scalar_cap and the roll-tenor relabel) re-normalizes
    to a DIFFERENT key — resolve to that so legacy keys keep working."""
    if key not in _RESULTS:
        key = run_lab(json.loads(key))
    return _RESULTS[key]


# ---------------------------------------------------------------------------
# Parameter sweeps — price-space Sharpe over a 2D grid
# ---------------------------------------------------------------------------

def _annualized_sharpe(pnl: pd.Series) -> float:
    pnl = pd.Series(pnl).astype(float).fillna(0.0)
    sd = pnl.std()
    if not np.isfinite(sd) or sd == 0:
        return np.nan
    return float(pnl.mean() / sd * np.sqrt(252.0))


def momentum_sweep(commodity: str) -> pd.DataFrame:
    """
    Price-space Sharpe over (fast MA × slow MA). Roll path is built once;
    each cell is one vectorized momentum() signal pass — no MTM loop.
    Returns DataFrame indexed by fast, columns slow.
    """
    from energy.strategies.momentum import momentum

    key = json.dumps({"sweep": "momentum", "commodity": commodity})
    if key in _SWEEPS:
        return _SWEEPS[key]

    prices, _, prices_full, expiry_cal = _load_commodity(commodity)
    roll_path = _build_roll(commodity, prices, expiry_cal)
    # CLEAN signal series, same recipe as _run_momentum (Pass 8 fix)
    sig_prices = _clean_signal_prices(commodity)

    grid = pd.DataFrame(index=MOMENTUM_SWEEP_FAST, columns=MOMENTUM_SWEEP_SLOW, dtype=float)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for fast in MOMENTUM_SWEEP_FAST:
            for slow in MOMENTUM_SWEEP_SLOW:
                if fast >= slow:
                    continue
                path = momentum(
                    prices=prices, rolled_df=roll_path, front_col="SIGNAL",
                    ma_pairs=[(fast, slow)], prices_signal=sig_prices,
                )
                # Booked via the sanctioned primitive: position[t-1] * pnl[t],
                # the same timing build_measures' MTM loop implements.
                grid.loc[fast, slow] = _annualized_sharpe(
                    held_pnl(path["position"], path["daily_pnl"], zero_first=False)
                )

    grid.index.name = "fast"
    grid.columns.name = "slow"
    _cache_put(_SWEEPS, _MAX_SWEEPS, key, grid)
    return grid


def stat_arb_sweep(pair: str, roll_tenor: str,
                    month_offset: int | None = None,
                    band_mode: str = "zscore") -> pd.DataFrame:
    """
    Price-space Sharpe over (lookback × entry threshold) for a pair.

    Uses the SAME construction as the single-run backtest (_run_stat_arb):
    delivery-month-matched legs via prepare_spread_legs (for strict pairs;
    per-leg rank path for rank_approximate pairs) and leg-level flow P&L via
    spread_pnl_from_legs — this used to run on the old independent-rank
    construction with no cross-leg coordination, silently testing a
    different (uncoordinated, since-disbanded) strategy than the panel next
    to it on the same page.

    Exit is always mean-cross (the sole exit rule since 2026-07-16).
    Legs/roll paths are prepared ONCE for the whole grid (energy's own
    param_sweep rebuilds them per cell, prohibitively slow for an
    interactive page).
    Returns DataFrame indexed by lookback, columns entry threshold.
    """
    from energy.strategies.spread_rolling import prepare_spread_legs, spread_signal_series
    from energy.strategies.relative_value.rv_zscore import _build_signal
    from energy.strategies.rolling import spread_pnl_from_legs
    from data import loader

    roll_tenor = _ROLL_TENOR_ALIASES.get(roll_tenor, roll_tenor)
    key = json.dumps({"sweep": "statarb", "pair": pair, "tenor": roll_tenor,
                       "month_offset": month_offset,
                       "band_mode": band_mode})
    if key in _SWEEPS:
        return _SWEEPS[key]

    leg1, leg2 = _pair_from_label(pair)
    roll_config = ROLL_TENORS[roll_tenor]

    leg1_df, leg2_df, _spread_meta = prepare_spread_legs(
        leg1, leg2,
        loader.get_prices_normalized(leg1), loader.get_prices_normalized(leg2),
        loader.get_expiry(_specs()[leg1]["ticker"]), loader.get_expiry(_specs()[leg2]["ticker"]),
        months1=_months_or_none(leg1), months2=_months_or_none(leg2),
        roll_config=roll_config, month_offset=month_offset,
    )
    # CLEAN flow-cumsum signal series — same recipe as _run_stat_arb and the
    # Signals/Levels cards (Pass 8 fix); cell-vs-backtest reconciliation
    # requires the identical signal input.
    spread = spread_signal_series(leg1_df, leg2_df)
    flow = spread_pnl_from_legs(leg1_df, leg2_df)
    unit_spread_pnl = flow["daily_pnl"]

    trade_mask = unit_spread_pnl.index >= pd.Timestamp(_TRADE_START)

    thresholds = (STAT_ARB_SWEEP_DOLLAR_THRESHOLDS if band_mode == "dollar"
                  else STAT_ARB_SWEEP_THRESHOLDS)
    grid = pd.DataFrame(index=STAT_ARB_SWEEP_LOOKBACKS,
                        columns=thresholds, dtype=float)
    for lb in STAT_ARB_SWEEP_LOOKBACKS:
        for thr in thresholds:
            # exit_threshold=0.0 (not None): identical signal construction
            # to _run_stat_arb — cell-vs-backtest reconciliation requires
            # the identical code path.
            sig = _build_signal(spread, lb, thr, exit_threshold=0.0,
                                band_mode=band_mode)["signal_raw"]
            # Booked via the sanctioned primitive on the trade window:
            # signal[t-1] * flow[t], first in-window day zeroed — exactly
            # the capital loop's booking (verified to the cent 2026-07-09).
            pnl = held_pnl(
                sig.reindex(unit_spread_pnl.index).fillna(0.0)[trade_mask],
                unit_spread_pnl[trade_mask],
            )
            grid.loc[lb, thr] = _annualized_sharpe(pnl)

    grid.index.name = "lookback"
    grid.columns.name = "entry"
    _cache_put(_SWEEPS, _MAX_SWEEPS, key, grid)
    return grid


def has_cached_sweep(params: dict) -> bool:
    """True if the sweep grid for these params is already computed (cheap probe
    so the page can redraw the crosshair reactively without recomputing)."""
    p = _sweep_params(params)
    if p["strategy"] == "Momentum":
        key = json.dumps({"sweep": "momentum", "commodity": p["commodity"]})
    elif p["strategy"] == "Stat-Arb":
        tenor = _ROLL_TENOR_ALIASES.get(p["roll_tenor"], p["roll_tenor"])
        key = json.dumps({"sweep": "statarb", "pair": p["pair"], "tenor": tenor,
                          "month_offset": p.get("month_offset"),
                          "band_mode": p.get("band_mode", "zscore")})
    else:
        return False
    return key in _SWEEPS


def _sweep_params(params: dict) -> dict:
    """DEFAULTS-merged sweep params with unset Stat-Arb fields resolved from
    the shared per-pair config — same resolution as normalize_params, so a
    bare sweep request shows the same default view as a bare run."""
    p = {**DEFAULTS, **params}
    if p["strategy"] == "Stat-Arb":
        cfg = pair_defaults(p["pair"])
        for field, cfg_key in (("month_offset", "month_offset"),
                               ("lookback", "lookback"),
                               ("entry", "entry")):
            if params.get(field) is None:
                p[field] = cfg[cfg_key]
    return p


def sweep_for(params: dict) -> tuple[pd.DataFrame, dict] | None:
    """
    Run/fetch the sweep matching a normalized param dict.
    Returns (grid, axis_info) or None for strategies without a sweep.
    """
    p = _sweep_params(params)
    if p["strategy"] == "Momentum":
        grid = momentum_sweep(p["commodity"])
        cur = MOMENTUM_TIERS.get(p.get("tier", "Averaged"), [(p.get("fast", 5), p.get("slow", 60))])
        # crosshair on the (single or first) MA pair of the current selection
        fast, slow = (int(p["fast"]), int(p["slow"])) if p.get("tier") == "Custom" else cur[0]
        return grid, dict(x_title="Slow MA (days)", y_title="Fast MA (days)",
                          cur_x=slow, cur_y=fast,
                          title=f"{p['commodity']} Momentum — Price-Space Sharpe (fast × slow)")
    if p["strategy"] == "Stat-Arb":
        band_mode = p.get("band_mode", "zscore")
        grid = stat_arb_sweep(p["pair"], p["roll_tenor"],
                              month_offset=p.get("month_offset"),
                              band_mode=band_mode)
        offset_val = p.get("month_offset") or 0
        title_offset = f" (offset {offset_val:+d})" if offset_val else ""
        unit = "$/bbl" if band_mode == "dollar" else "σ"
        band_tag = " / $-band" if band_mode == "dollar" else ""
        return grid, dict(x_title=f"Entry threshold ({unit})", y_title="Lookback (days)",
                          cur_x=float(p["entry"]), cur_y=int(p["lookback"]),
                          title=f"{p['pair']}{title_offset}{band_tag} — Price-Space Sharpe (lookback × ε)")
    return None


# ---------------------------------------------------------------------------
# Analytics tables
# ---------------------------------------------------------------------------

def diagnostics(result: dict) -> pd.DataFrame:
    """
    MTM diagnostics split into Full Sample / Pre-Ukraine / Post-Ukraine columns.
    Rows: CAGR, Vol (ann.), Sharpe, Max Drawdown, Total PnL.
    """
    from energy.analytics.metrics import metrics as mtm_metrics_fn

    mtm = result["mtm"]
    split = pd.Timestamp(UKRAINE_SPLIT)
    samples = {
        "Full Sample": mtm,
        "Pre-Ukraine": mtm[mtm.index < split],
        "Post-Ukraine": mtm[mtm.index >= split],
    }

    rows = {"CAGR": {}, "Vol (ann.)": {}, "Sharpe": {}, "Max Drawdown": {}, "Total PnL ($)": {}}
    for label, df in samples.items():
        try:
            m = mtm_metrics_fn(df)
            rows["CAGR"][label] = f"{m['CAGR']:.1%}" if pd.notna(m["CAGR"]) else "—"
            rows["Vol (ann.)"][label] = f"{m['Std Dev (ann.)']:.1%}" if pd.notna(m["Std Dev (ann.)"]) else "—"
            rows["Sharpe"][label] = f"{m['Sharpe']:.2f}" if pd.notna(m["Sharpe"]) else "—"
            rows["Max Drawdown"][label] = f"{m['Drawdown']:.1%}" if pd.notna(m["Drawdown"]) else "—"
            rows["Total PnL ($)"][label] = f"{m['Total PnL']:,.0f}" if pd.notna(m["Total PnL"]) else "—"
        except Exception:
            for r in rows:
                rows[r][label] = "—"

    out = pd.DataFrame(rows).T.reset_index().rename(columns={"index": "Metric"})
    return out[["Metric", "Full Sample", "Pre-Ukraine", "Post-Ukraine"]]


def split_metrics(result: dict) -> pd.DataFrame:
    """
    Sample-split analytics table: Full Sample, Pre/Post-Ukraine, then one row
    per calendar year. Columns are "PS::<metric>" (price space) and
    "MTM::<metric>" — recomputed on each slice via the existing energy
    analytics functions, no new math.
    """
    from energy.analytics.metrics import legacy_capstone_metrics, metrics as mtm_metrics_fn

    ps = result["price_space"]
    mtm = result["mtm"]

    def _row(label: str, ps_slice: pd.DataFrame, mtm_slice: pd.DataFrame) -> dict:
        row: dict = {"Sample": label}
        try:
            for k, v in legacy_capstone_metrics(ps_slice, contracts=1, units=1).items():
                row[f"PS::{k}"] = v
        except Exception:
            pass
        try:
            for k, v in mtm_metrics_fn(mtm_slice).items():
                row[f"MTM::{k}"] = v
        except Exception:
            pass
        return row

    split = pd.Timestamp(UKRAINE_SPLIT)
    rows = [
        _row("Full Sample", ps, mtm),
        _row(f"Pre-Ukraine (< {UKRAINE_SPLIT})", ps[ps.index < split], mtm[mtm.index < split]),
        _row(f"Post-Ukraine (≥ {UKRAINE_SPLIT})", ps[ps.index >= split], mtm[mtm.index >= split]),
    ]
    for yr in sorted(ps.index.year.unique()):
        ps_y = ps[ps.index.year == yr]
        mtm_y = mtm[mtm.index.year == yr]
        if len(ps_y) >= 2:
            rows.append(_row(str(yr), ps_y, mtm_y))

    return pd.DataFrame(rows)

"""
Dual-measure accounting: price_space_measure and mtm_measure.

Every strategy output should be viewed in both dimensions:
  1. price_space_measure — raw signed PnL in $/unit (price units), no capital sizing.
     Shows the pure signal / spread behavior without position-sizing distortion.
  2. mtm_measure         — capital account with vol-targeted position sizing.
     Shows risk-adjusted performance; the vol_scalar column directly reveals
     when/how much the vol spec is distorting results vs price_space.

Usage — directional strategy (signal in {-1, 0, +1}):
    out = build_measures(
        daily_pnl   = mom_path["daily_pnl"],
        ref_price   = build_held_price_series(mom_path, prices),
        signal      = mom_path["position"],
        rebalance_flag      = mom_path["rebalance_flag"],
        t_cost_abs          = spec["t_cost_abs"],
        initial_capital     = CAPITAL,
        contract_multiplier = spec["contract_multiplier"],
        vol_window          = VOL_WINDOW,
        vol_target_ann      = VOL_TARGET,
    )

Usage — long-only rolling (no signal):
    out = build_measures(
        daily_pnl   = path_df["daily_pnl"],
        ref_price   = build_held_price_series(path_df, prices),
        rebalance_flag      = path_df["roll_day_flag"],
        initial_capital     = CAPITAL,
        contract_multiplier = spec["contract_multiplier"],
        vol_window          = VOL_WINDOW,
        vol_target_ann      = VOL_TARGET,
    )

Usage — spread (two-leg, no explicit signal):
    out = build_measures(
        daily_pnl   = rolled["daily_pnl"],
        ref_price   = rolled["leg1_price"],
        t_cost      = rolled["t_cost"],
        initial_capital     = CAPITAL,
        contract_multiplier = spec["contract_multiplier"],
        vol_window          = VOL_WINDOW,
        vol_target_ann      = VOL_TARGET,
    )

out["price_space"]         — DataFrame: daily_pnl, net_pnl, cum_pnl, cum_net_pnl
out["price_space_metrics"] — pd.Series: Sharpe, CAGR, Drawdown, ...
out["mtm"]                 — DataFrame: capital, equity_index, contracts, vol_scalar, ...
out["mtm_metrics"]         — pd.Series: same set in capital-account terms
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from energy.accounting.booking import held, held_pnl
from energy.analytics.metrics import legacy_capstone_metrics, metrics as _mtm_metrics
from energy.sizing.daily_size import _scalar_from_valid


def build_measures(
    daily_pnl: pd.Series,
    ref_price: pd.Series,
    *,
    ref_price2: pd.Series | None = None,
    signal: pd.Series | None = None,
    t_cost: pd.Series | float = 0.0,
    t_cost_abs: float = 0.0,
    initial_capital: float = 1_000_000,
    contract_multiplier: float = 1_000,
    vol_window: int = 120,
    vol_target_ann: float = 0.15,
    vol_floor: float = 1e-8,
    vol_scalar_cap: float | None = None,
    rebalance_flag: pd.Series | None = None,
    warmup_returns: pd.Series | None = None,
) -> dict:
    """
    Compute both accounting views for any strategy PnL series.

    Parameters
    ----------
    daily_pnl : Series
        Unsigned daily PnL in $/unit (price change of held contract).
        For spreads: already signed spread daily_pnl.
    ref_price : Series
        Reference price for MTM position sizing.
        Single-leg strategies: held_price (from build_held_price_series).
        Spreads: leg1_price (near-leg held price).
    ref_price2 : Series or None
        Optional second-leg reference price for spread strategies.
        When provided, contract sizing uses (ref_price + ref_price2) / 2
        so that capital is split 50-50 across both legs correctly.
        Single-leg strategies should leave this as None.
    signal : Series or None
        Optional direction signal in {-1, 0, +1}.
        When provided:
          - price_space uses daily_pnl * signal (signed PnL)
          - MTM contracts are signed accordingly
        When None: strategy is treated as long-only.
    t_cost : Series or float
        Per-unit roll transaction cost ($/unit). Typically the t_cost
        column from a roll path or spread output. Scaled by contracts
        held in the MTM loop.
    t_cost_abs : float
        Absolute $/unit cost per unit of CONTRACT TURNOVER in the MTM
        loop (mirrors mtm_from_path's t_cost_abs). Applied on top of t_cost.
    initial_capital : float
        Starting capital for the MTM measure (dollars).
    contract_multiplier : float
        Dollars per unit price move per contract (e.g. 1000 BBL/contract).
    vol_window : int
        Lookback window (trading days) for trailing vol in MTM sizing.
        0 = disable vol targeting.  Realized vol is measured on the
        instrument's own daily returns (daily_pnl / previous ref_price),
        independent of whether a position is open — never on the strategy's
        own equity returns, which are zero while flat and collapse the
        estimate over flat streaks.
    vol_target_ann : float
        Annualised vol target for MTM measure (e.g. 0.15 = 15%).
    vol_floor : float
        Minimum realized vol to apply scaling (avoids div-by-zero).
    vol_scalar_cap : float or None
        Hard ceiling on the vol scalar (None = uncapped). Same semantics as
        _stat_arb_capital_loop's cap: the uncapped formula raises leverage
        exactly when trailing vol is low (chop), and the cap bounds that
        mechanism. One shared convention across every engine.
    rebalance_flag : Series or None
        Days to resize the MTM position. None = resize daily.
        Pass path_df["rebalance_flag"] for signal strategies, or
        path_df["roll_day_flag"] for long-only rolling.
    warmup_returns : Series or None
        Pre-sample daily returns used to seed the vol estimator so the
        vol scalar is already calibrated on day 0 (no cold-start at 1.0).
        MUST be in the same space as the in-sample estimator input —
        roll-aware flow over previous held price (daily_pnl / held_price
        lagged), never a raw generic column's pct_change: raw generics jump
        at every re-rank, which inflates the seeded vol with phantom moves
        (same defect class as the 2026-07-14 signal-series fix).

    Returns
    -------
    dict with keys:
        price_space         : DataFrame (daily_pnl, net_pnl, cum_pnl, cum_net_pnl)
        price_space_metrics : pd.Series
        mtm                 : DataFrame (capital, equity_index, contracts,
                              daily_ret, vol_scalar, realized_vol_ann,
                              dollar_pnl, txn_cost_mtm)
        mtm_metrics         : pd.Series
    """
    # --- align all inputs to common index ---
    common = daily_pnl.index.intersection(ref_price.index)
    pnl   = daily_pnl.reindex(common).astype(float)
    if ref_price2 is not None:
        price = ((ref_price.reindex(common) + ref_price2.reindex(common)) / 2.0).astype(float)
    else:
        price = ref_price.reindex(common).astype(float)

    if signal is not None:
        sig = signal.reindex(common).fillna(0.0).astype(float)
    else:
        sig = pd.Series(1.0, index=common)

    if isinstance(t_cost, pd.Series):
        tc = t_cost.reindex(common).fillna(0.0).astype(float)
    else:
        tc = pd.Series(float(t_cost), index=common)

    if rebalance_flag is not None:
        rflag = rebalance_flag.reindex(common).fillna(0).astype(int)
    else:
        rflag = pd.Series(1, index=common)   # daily rebalance

    n = len(common)
    pnl_arr   = pnl.to_numpy(float)
    price_arr = price.to_numpy(float)
    sig_arr   = sig.to_numpy(float)
    tc_arr    = tc.to_numpy(float)
    rflag_arr = rflag.to_numpy(int)

    # =========================================================
    # 1. PRICE SPACE MEASURE
    # =========================================================
    # Booked via the sanctioned primitive (energy.accounting.booking):
    # pnl[t] * sig[t-1], the same timing as the MTM loop below (contracts
    # held INTO t earn t's flow). Long-only callers (sig = 1) only lose
    # day 0, which the MTM loop never books either.
    sig_held   = held(sig)
    signed_pnl = held_pnl(sig, pnl, zero_first=False)  # NaN flows stay NaN
    signed_tc  = tc * sig_held.abs()   # costs are always negative, abs() for magnitude

    ps = pd.DataFrame(index=common)
    ps["daily_pnl"]    = signed_pnl
    ps["t_cost"]       = signed_tc
    ps["net_pnl"]      = signed_pnl + signed_tc
    ps["cum_pnl"]      = signed_pnl.cumsum()
    ps["cum_net_pnl"]  = ps["net_pnl"].cumsum()

    ps_metrics = legacy_capstone_metrics(ps, contracts=1, units=1)

    # =========================================================
    # 2. MTM MEASURE  (vol-targeted capital account)
    # =========================================================
    # Pre-sample returns for vol warm-up
    if warmup_returns is not None:
        warmup_arr = warmup_returns.dropna().to_numpy(float)
    else:
        warmup_arr = np.array([], dtype=float)

    contracts        = np.full(n, np.nan, dtype=float)
    dollar_pnl_arr   = np.zeros(n, dtype=float)
    txn_cost_arr     = np.zeros(n, dtype=float)
    capital_arr      = np.full(n, np.nan, dtype=float)
    daily_ret_arr    = np.full(n, np.nan, dtype=float)
    vol_scalar_arr   = np.ones(n, dtype=float)
    realized_vol_arr = np.full(n, np.nan, dtype=float)

    # Vol-estimator input: the instrument's own daily return, position-
    # independent.  pnl / prev_price is the fractional return the account
    # would earn per unit of vol scalar if fully invested (contracts =
    # capital / (price * mult)), and it stays defined while the strategy is
    # flat.  Sizing on the strategy's own equity returns is forbidden here:
    # flat streaks write exact zeros into the window, collapse the vol
    # estimate, and blow up position size at re-entry (2021-06-14 /
    # 2025-08-25 sizing spikes).  Same space as warmup_returns (price
    # pct-change), which the equity-return stream never was.
    px_prev = pd.Series(price_arr, dtype=float).ffill().shift(1).to_numpy()
    px_prev = np.where(px_prev > 0, px_prev, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        unit_ret_arr = pnl_arr / px_prev

    def _vol_scalar(t: int) -> float:
        if vol_window == 0:
            return 1.0
        in_hist  = unit_ret_arr[max(1, t - vol_window) : t]
        valid_in = in_hist[~np.isnan(in_hist)]
        need     = vol_window - len(valid_in)
        if need > 0 and len(warmup_arr) > 0:
            wu    = warmup_arr[-need:]
            valid = np.concatenate([wu[~np.isnan(wu)], valid_in])
        else:
            valid = valid_in
        if len(valid) < max(2, vol_window // 2):
            return 1.0
        # Judgment call: measures.py keeps its own warmup/fixed-window/min_obs logic
        # and delegates only the pure formula to _scalar_from_valid.
        scalar, rv = _scalar_from_valid(valid, vol_target_ann, vol_floor)
        realized_vol_arr[t] = rv
        if vol_scalar_cap is not None:
            scalar = min(scalar, vol_scalar_cap)
        return scalar

    def _target_contracts(cap: float, px: float, sv: float, direction: float) -> float:
        if np.isnan(px) or px <= 0:
            return 0.0
        return direction * sv * cap / (px * contract_multiplier)

    # Day 0: size using warmup vol if available, else unscaled
    def _warmup_vol_scalar() -> float:
        if vol_window == 0 or len(warmup_arr) == 0:
            return 1.0
        w = warmup_arr[-vol_window:]
        w = w[~np.isnan(w)]
        if len(w) < max(2, vol_window // 2):
            return 1.0
        scalar, _ = _scalar_from_valid(w, vol_target_ann, vol_floor)
        if vol_scalar_cap is not None:
            scalar = min(scalar, vol_scalar_cap)
        return scalar

    px0  = price_arr[0]
    sig0 = sig_arr[0]
    sv0  = _warmup_vol_scalar()
    c0   = _target_contracts(initial_capital, px0, sv0, sig0)
    entry_cost = abs(c0) * t_cost_abs
    capital_arr[0] = initial_capital - entry_cost
    txn_cost_arr[0] = -entry_cost
    contracts[0] = _target_contracts(capital_arr[0], px0, sv0, sig0)
    vol_scalar_arr[0] = sv0

    for t in range(1, n):
        prev_c = contracts[t - 1]

        # PnL: unsigned roll PnL * direction baked into contracts sign
        dollar_pnl_arr[t] = pnl_arr[t] * contract_multiplier * prev_c
        # Roll t_cost from path ($/unit * |contracts|)
        roll_tc_dollar = tc_arr[t] * contract_multiplier * abs(prev_c)
        capital_pre = capital_arr[t - 1] + dollar_pnl_arr[t] + roll_tc_dollar

        daily_ret_arr[t] = (
            (dollar_pnl_arr[t] + roll_tc_dollar) / capital_arr[t - 1]
            if capital_arr[t - 1] > 0
            else np.nan
        )

        if rflag_arr[t] == 1:
            sv = _vol_scalar(t)
            vol_scalar_arr[t] = sv
            new_c = _target_contracts(capital_pre, price_arr[t], sv, sig_arr[t])
            # t_cost_abs on contract turnover
            turnover_cost = abs(new_c - prev_c) * t_cost_abs
            capital_arr[t] = capital_pre - turnover_cost
            txn_cost_arr[t] = roll_tc_dollar - turnover_cost
            # Recompute after cost
            contracts[t] = _target_contracts(capital_arr[t], price_arr[t], sv, sig_arr[t])
        else:
            vol_scalar_arr[t] = vol_scalar_arr[t - 1]
            contracts[t]      = prev_c
            capital_arr[t]    = capital_pre
            txn_cost_arr[t]   = roll_tc_dollar

    capital_s = pd.Series(capital_arr, index=common, name="capital")

    mtm = pd.DataFrame(index=common)
    mtm["contracts"]        = contracts
    mtm["ref_price"]        = price_arr
    mtm["dollar_pnl"]       = dollar_pnl_arr
    mtm["txn_cost_mtm"]     = txn_cost_arr
    mtm["capital"]          = capital_s
    mtm["equity_index"]     = capital_s / initial_capital
    mtm["daily_ret"]        = daily_ret_arr
    mtm["vol_scalar"]       = vol_scalar_arr
    mtm["realized_vol_ann"] = realized_vol_arr * np.sqrt(252.0)

    mtm_m = _mtm_metrics(mtm)

    return {
        "price_space":         ps,
        "price_space_metrics": ps_metrics,
        "mtm":                 mtm,
        "mtm_metrics":         mtm_m,
    }

"""
rv_regression.py
----------------
Regression-based relative-value strategy for energy spreads.

Constructs a stationary "synthetic spread" via rolling OLS on price levels:

    P₀(t) = α + β₁·P₁(t) + … + βₙ·Pₙ(t) + ε(t)

The OLS fit uses strictly prior data (no lookahead).  The out-of-sample
residual ε_t = P₀(t) − α̂ − Σ β̂ᵢ·Pᵢ(t) is z-scored against the in-sample
residual distribution to generate the entry/exit signal.

Weight convention  :  W = [1, −β̂₁, …, −β̂ₙ] normalised so Σ|Wᵢ| = 1
Capital allocation :  contracts_i = Wᵢ · signal · scalar · capital / (Pᵢ · multᵢ)

Works for 2-leg cross-commodity pairs (dynamic hedge-ratio stat-arb) and
N-leg calendar / cross-commodity baskets.

Public API
----------
rv_regression(legs, prices_list, expiry_list, ...)  ->  dict
    keys: strategy_df, signal_df, weights_df, residuals, leg_dfs, held_prices

Shared utility (also imported by rv_pca.py)
-------------------------------------------
_n_leg_capital_loop(...)   ->  pd.DataFrame
_build_residual_signal(...)  ->  pd.DataFrame
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from energy.accounting.contract_specs import CONTRACT_SPECS
from energy.strategies.relative_value.rv_zscore import _prepare_leg


# ============================================================
# SHARED: N-LEG CAPITAL LOOP
# ============================================================
def _n_leg_capital_loop(
    signal_raw: pd.Series,
    weights_df: pd.DataFrame,
    pnl_df: pd.DataFrame,
    held_prices_df: pd.DataFrame,
    multipliers: np.ndarray,
    t_costs: np.ndarray,
    initial_capital: float,
    roll_day_flag: pd.Series | None = None,
    vol_window: int = 0,
    vol_target_ann: float = 0.15,
    vol_min_obs: int | None = None,
    vol_floor: float = 1e-8,
) -> pd.DataFrame:
    """
    General N-leg MTM capital loop.

    Position sizing
    ---------------
    When signal s ∈ {−1, +1} and vol scalar sc:

        contracts_i = Wᵢ · s · sc · capital / (Pᵢ · multᵢ)

    where Wᵢ are normalised weights (Σ|Wᵢ| = 1).  This generalises the
    ±0.5 convention from 2-leg strategies: for equal-weight 2 legs,
    W = [+0.5, −0.5] recovers the original formula.

    Rebalance triggers
    ------------------
    - Signal change  (entry / exit / flip)
    - Roll day on any leg (union of leg roll flags), when position is live
    - Day 0 (always)

    Parameters
    ----------
    signal_raw      : {−1, 0, +1} Series on the trading calendar
    weights_df      : normalised per-leg weights (Σ|Wᵢ|=1), columns = leg names
    pnl_df          : per-contract daily PnL per leg, same calendar
    held_prices_df  : held price per leg, same calendar
    multipliers     : contract multiplier per leg, shape (n_legs,)
    t_costs         : absolute t-cost per contract per leg, shape (n_legs,)
    roll_day_flag   : union roll flag series (1 = any leg rolling)
    vol_window      : trailing window for vol targeting (0 = off); realized
                      vol is measured on the weighted basket's own returns,
                      independent of whether a position is open
    vol_target_ann  : annualised vol target (used only when vol_window > 0)
    """
    idx       = signal_raw.index
    n         = len(idx)
    n_legs    = len(weights_df.columns)
    leg_names = weights_df.columns.tolist()

    if vol_min_obs is None:
        vol_min_obs = vol_window

    target_daily_vol = vol_target_ann / np.sqrt(252.0)

    sig   = signal_raw.to_numpy(dtype=float)
    W     = weights_df.reindex(idx).to_numpy(dtype=float)          # (n, n_legs)
    pnls  = pnl_df.reindex(idx).fillna(0.0).to_numpy(dtype=float)  # (n, n_legs)
    pxs   = held_prices_df.reindex(idx).to_numpy(dtype=float)      # (n, n_legs)
    mults = np.asarray(multipliers, dtype=float)                    # (n_legs,)
    tcs   = np.asarray(t_costs, dtype=float)                       # (n_legs,)

    if roll_day_flag is not None:
        roll = roll_day_flag.reindex(idx).fillna(0).astype(int).to_numpy()
    else:
        roll = np.zeros(n, dtype=int)

    # Rebalance when signal changes, or on a roll day while in a live position
    prev_sig   = np.concatenate([[0.0], sig[:-1]])
    sig_change = (sig != prev_sig).astype(int)
    active     = (sig != 0.0).astype(int)
    live_roll  = ((roll == 1) & (active == 1)).astype(int)
    reb        = np.maximum(sig_change, live_roll)
    reb[0]     = 1

    # Forward-fill prices to cover roll-day gaps (O(N·L))
    def _ffill_prices(arr: np.ndarray) -> np.ndarray:
        out = arr.copy()
        for j in range(arr.shape[1]):
            last = np.nan
            for i in range(arr.shape[0]):
                v = out[i, j]
                if not np.isnan(v) and v > 0.0:
                    last = v
                out[i, j] = last
        return out

    pxs_safe = _ffill_prices(pxs)

    reb_times = np.where(reb)[0]

    contracts  = np.zeros((n, n_legs), dtype=float)
    dollar_pnl = np.zeros((n, n_legs), dtype=float)
    txn_cost   = np.zeros(n, dtype=float)
    capital    = np.full(n, np.nan, dtype=float)
    daily_ret  = np.full(n, np.nan, dtype=float)
    self_vol   = np.full(n, np.nan, dtype=float)
    vol_scalar = np.ones(n, dtype=float)

    # Vol-estimator input: the weighted basket's own daily return,
    # position-independent.  Σ Wᵢ · pnlᵢ / prev_Pᵢ is the fractional return
    # the account would earn per unit of vol scalar if fully invested, and it
    # stays defined while the strategy is flat.  Sizing on the strategy's own
    # equity returns (daily_ret) is forbidden here: flat streaks write exact
    # zeros into the window, collapse the vol estimate, and blow up position
    # size at re-entry.
    pxs_prev = np.vstack([np.full((1, n_legs), np.nan), pxs_safe[:-1]])
    with np.errstate(divide="ignore", invalid="ignore"):
        basket_ret = np.sum(W * pnls / pxs_prev, axis=1)

    prev_contracts = np.zeros(n_legs, dtype=float)
    prev_cap       = initial_capital

    for seg_i, t_s in enumerate(reb_times):
        t_e = int(reb_times[seg_i + 1]) if seg_i + 1 < len(reb_times) else n

        # ── PnL at rebalance point using previous-segment contracts ──
        if t_s > 0:
            dp             = prev_contracts * pnls[t_s] * mults
            dollar_pnl[t_s] = dp
            gross          = float(dp.sum())
            cap_pre        = prev_cap + gross
            daily_ret[t_s] = gross / prev_cap if prev_cap > 0 else np.nan
        else:
            cap_pre = prev_cap

        # ── Vol scalar (only when vol_window > 0) ──
        if vol_window > 0:
            hist  = basket_ret[:t_s]
            valid = hist[~np.isnan(hist)]
            if len(valid) >= vol_min_obs:
                realized = float(valid[-vol_window:].std(ddof=1))
                self_vol[t_s] = realized
                sc = 1.0 if (np.isnan(realized) or realized < vol_floor) else target_daily_vol / realized
            else:
                sc = 1.0
        else:
            sc = 1.0
        vol_scalar[t_s] = sc

        # ── New contracts based on current weights ──
        s_t  = sig[t_s]
        w_t  = W[t_s]
        px_t = pxs_safe[t_s]

        if s_t != 0.0 and not np.any(np.isnan(w_t)) and cap_pre > 0.0:
            with np.errstate(invalid="ignore", divide="ignore"):
                new_c = s_t * sc * cap_pre * w_t / (px_t * mults)
            new_c = np.where(np.isfinite(new_c), new_c, 0.0)
        else:
            new_c = np.zeros(n_legs, dtype=float)

        cost_t    = float(np.sum(np.abs(new_c - prev_contracts) * tcs))
        cap_after = cap_pre - cost_t

        contracts[t_s]  = new_c
        txn_cost[t_s]   = cost_t
        capital[t_s]    = cap_after

        # ── Vectorize the between-rebalance segment [t_s+1, t_e) ──
        seg = np.arange(t_s + 1, t_e)
        if len(seg):
            contracts[seg]  = new_c[np.newaxis, :]
            vol_scalar[seg] = sc

            dp_seg          = new_c[np.newaxis, :] * pnls[seg] * mults[np.newaxis, :]
            dollar_pnl[seg] = dp_seg
            gross_seg       = dp_seg.sum(axis=1)
            cum_gross       = np.cumsum(gross_seg)
            capital[seg]    = cap_after + cum_gross

            prev_caps       = np.empty(len(seg))
            prev_caps[0]    = cap_after
            prev_caps[1:]   = cap_after + cum_gross[:-1]
            daily_ret[seg]  = np.where(prev_caps > 0, gross_seg / prev_caps, np.nan)

        prev_contracts = new_c
        prev_cap       = float(capital[t_e - 1]) if t_e > 0 else cap_after

    cap_s = pd.Series(capital, index=idx, name="capital")

    out = pd.DataFrame(index=idx)
    out["signal_raw"] = sig
    for j, name in enumerate(leg_names):
        out[f"weight_{name}"]     = W[:, j]
        out[f"contracts_{name}"]  = contracts[:, j]
        out[f"dollar_pnl_{name}"] = dollar_pnl[:, j]
    out["gross_pnl"]    = dollar_pnl.sum(axis=1)
    out["t_cost"]       = txn_cost
    out["net_pnl"]      = out["gross_pnl"] - out["t_cost"]
    out["capital"]      = cap_s
    out["daily_ret"]    = pd.Series(daily_ret, index=idx)
    out["equity_index"] = cap_s / initial_capital
    out["roll_day_flag"] = roll
    out["self_vol"]     = pd.Series(self_vol, index=idx)
    out["vol_scalar"]   = pd.Series(vol_scalar, index=idx)

    return out


# ============================================================
# ROLLING OLS
# ============================================================
def _rolling_ols(
    prices_df: pd.DataFrame,
    lookback: int,
    intercept: bool = True,
) -> tuple[pd.DataFrame, pd.Series]:
    """
    Rolling OLS: P₀ ~ α + β₁·P₁ + … + βₙ·Pₙ

    Uses strictly prior window to avoid lookahead:
      Fit on [t−lookback, t−1]  →  predict residual at t.

    Returns
    -------
    weights_df : (n × n_legs) DataFrame of normalised weights (Σ|Wᵢ|=1)
                 W_raw = [1, −β̂₁, …, −β̂ₙ]
    residuals  : (n,) Series of out-of-sample OLS residuals
    """
    cols   = prices_df.columns.tolist()
    n_legs = len(cols)
    n      = len(prices_df)

    P     = prices_df.to_numpy(dtype=float)  # (n, n_legs)
    Y     = P[:, 0]
    X_raw = P[:, 1:]                          # (n, n_legs-1)

    W_out = np.full((n, n_legs), np.nan)
    resid = np.full(n, np.nan)

    for t in range(lookback, n):
        sl  = slice(t - lookback, t)           # [t-lookback, t-1] — no lookahead
        Y_w = Y[sl]
        X_w = X_raw[sl]

        if np.any(np.isnan(Y_w)) or np.any(np.isnan(X_w)):
            continue

        X_fit = np.hstack([np.ones((lookback, 1)), X_w]) if intercept else X_w

        try:
            coeffs, _, _, _ = np.linalg.lstsq(X_fit, Y_w, rcond=None)
        except np.linalg.LinAlgError:
            continue

        if intercept:
            alpha, betas = coeffs[0], coeffs[1:]
        else:
            alpha, betas = 0.0, coeffs

        # Out-of-sample residual at t (not used in the fit)
        resid[t] = Y[t] - alpha - float(X_raw[t] @ betas)

        # Weights [1, -β₁, ..., -βₙ] normalised to Σ|Wᵢ|=1
        W_raw    = np.empty(n_legs)
        W_raw[0] = 1.0
        W_raw[1:] = -betas
        norm     = float(np.sum(np.abs(W_raw)))
        W_out[t] = W_raw / norm if norm > 1e-12 else np.full(n_legs, np.nan)

    weights_df = pd.DataFrame(W_out, index=prices_df.index, columns=cols)
    residuals  = pd.Series(resid, index=prices_df.index, name="regression_residual")
    return weights_df, residuals


# ============================================================
# SHARED: SIGNAL ON RESIDUAL / BASKET SERIES
# ============================================================
def _build_residual_signal(
    series: pd.Series,
    lookback: int,
    zscore_threshold: float = 1.0,
) -> pd.DataFrame:
    """
    Z-score of a residual / basket series → mean-reversion entry signal.

    Rolling mean and std computed over the prior `lookback` observations.
    Since the residuals are already out-of-sample, no second lookahead occurs.

    Signal convention (identical to rv_zscore):
      +1  long basket   (long leg₀ / short hedge legs)
      −1  short basket
       0  flat

    Entry : |z_{t-1}| > zscore_threshold
    Exit  : series crosses back through rolling mean
    """
    s = series.astype(float)

    roll_mean = s.rolling(window=lookback, min_periods=lookback).mean()
    roll_std  = s.rolling(window=lookback, min_periods=lookback).std()
    denom     = roll_std.replace(0.0, np.nan)
    zscore    = (s - roll_mean) / denom

    upper = roll_mean + zscore_threshold * roll_std
    lower = roll_mean - zscore_threshold * roll_std

    n      = len(s)
    zs_arr = zscore.to_numpy(dtype=float)
    m_arr  = roll_mean.to_numpy(dtype=float)
    s_arr  = s.to_numpy(dtype=float)
    sig    = np.zeros(n, dtype=float)
    state  = 0.0

    for i in range(1, n):
        pz = zs_arr[i - 1]
        ps = s_arr[i - 1]
        pm = m_arr[i - 1]

        if np.isnan(pz) or np.isnan(ps) or np.isnan(pm):
            state = 0.0
        elif state == 0.0:
            if pz >= zscore_threshold:
                state = -1.0
            elif pz <= -zscore_threshold:
                state = 1.0
        elif state == 1.0:
            if ps >= pm:
                state = 0.0
        elif state == -1.0:
            if ps <= pm:
                state = 0.0
        sig[i] = state

    out = pd.DataFrame({
        "residual":   s,
        "resid_mean": roll_mean,
        "resid_std":  roll_std,
        "zscore":     zscore,
        "upper_band": upper,
        "lower_band": lower,
        "signal_raw": pd.Series(sig, index=s.index),
    })
    valid_mask = roll_mean.notna()
    out.attrs["start_date"] = (
        out.index[int(np.argmax(valid_mask.to_numpy()))] if valid_mask.any() else out.index[0]
    )
    return out


# ============================================================
# HELPERS
# ============================================================
def _build_held_prices(
    legs: list[str],
    leg_dfs: list[pd.DataFrame],
    idx: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Extract held prices on idx; return only rows where all legs are non-null."""
    df = pd.DataFrame(
        {name: df["held_price"].reindex(idx) for name, df in zip(legs, leg_dfs)}
    )
    valid = df.notna().all(axis=1)
    return df.loc[valid]


def _union_roll_flag(
    leg_dfs: list[pd.DataFrame],
    idx: pd.DatetimeIndex,
) -> pd.Series:
    flags = [df["roll_day_flag"].reindex(idx).fillna(0).astype(int) for df in leg_dfs]
    arr   = np.max(np.column_stack([f.to_numpy() for f in flags]), axis=1)
    return pd.Series(arr, index=idx, name="roll_day_flag")


# ============================================================
# PUBLIC API
# ============================================================
def rv_regression(
    legs: list[str],
    prices_list: list[pd.DataFrame],
    expiry_list: list[pd.Series],
    initial_capital: float = 1_000_000.0,
    lookback: int = 60,
    zscore_threshold: float = 1.0,
    vol_window: int = 0,
    vol_target_ann: float = 0.15,
    trade_start: str | None = None,
    roll_config: str = "prompt_EOM_roll",
    intercept: bool = True,
) -> dict:
    """
    Regression-based N-leg RV strategy.

    legs[0] is the "dependent" variable in the OLS regression.
    The beta-weighted residual of legs[0] on legs[1:] is the traded spread.

    For a 2-leg pair this implements dynamic-hedge-ratio stat-arb
    (generalises rv_zscore which assumes hedge ratio = 1).

    For N legs this creates a beta-neutral basket similar to a PnL-optimised
    curve butterfly or cross-commodity spread.

    Parameters
    ----------
    legs             : commodity names, e.g. ["WTI", "Brent"]
    prices_list      : raw price DataFrames, same order as legs
    expiry_list      : expiry Series, same order as legs
    lookback         : OLS estimation window (also z-score window)
    zscore_threshold : entry threshold for |z-score of residual|
    intercept        : include constant in OLS (recommended)
    vol_window       : trailing window for vol targeting on the basket's own
                       returns, position-independent (0 = off)
    vol_target_ann   : annualised vol target used when vol_window > 0
    trade_start      : ISO date — equity index = 1.0 at this date
    roll_config      : CONTRACT_SPECS key for roll path construction

    Returns
    -------
    dict
        strategy_df : daily strategy with contracts, PnL, capital, equity_index
        signal_df   : residual, zscore, bands, signal_raw
        weights_df  : normalised per-leg OLS weights on the full calendar
        residuals   : out-of-sample OLS residual Series
        leg_dfs     : roll path DataFrames per leg (same order as legs)
        held_prices : DataFrame of held prices on the clean intersection calendar
    """
    if len(legs) < 2:
        raise ValueError("Need at least 2 legs.")
    if len(legs) != len(prices_list) or len(legs) != len(expiry_list):
        raise ValueError("legs, prices_list, expiry_list must have the same length.")

    # ── Roll paths ──
    leg_dfs = [
        _prepare_leg(name, px, exp, roll_config)
        for name, px, exp in zip(legs, prices_list, expiry_list)
    ]

    # ── Strict intersection calendar ──
    idx = leg_dfs[0].index
    for df in leg_dfs[1:]:
        idx = idx.intersection(df.index)
    idx = idx.sort_values()

    held_prices_df = _build_held_prices(legs, leg_dfs, idx)
    idx = held_prices_df.index  # trimmed to fully-clean rows

    # ── Contract specs ──
    specs = [CONTRACT_SPECS.get(n, {}) for n in legs]
    mults = np.array([float(s.get("contract_multiplier", 1)) for s in specs])
    tcs   = np.array([float(s.get("t_cost_abs", 0.0)) for s in specs])

    # ── Rolling OLS ──
    weights_df, residuals = _rolling_ols(held_prices_df, lookback, intercept=intercept)

    # ── Signal ──
    signal_df = _build_residual_signal(residuals, lookback, zscore_threshold)

    # ── Clip to trade_start ──
    trade_idx = idx[idx >= pd.Timestamp(trade_start)] if trade_start else idx

    signal_raw  = signal_df["signal_raw"].reindex(trade_idx).fillna(0.0)
    weights_td  = weights_df.reindex(trade_idx)
    prices_td   = held_prices_df.reindex(trade_idx)
    pnl_df      = pd.DataFrame({
        name: df["daily_pnl"].reindex(trade_idx).fillna(0.0)
        for name, df in zip(legs, leg_dfs)
    })
    roll_flag   = _union_roll_flag(leg_dfs, trade_idx)

    # ── Capital loop ──
    strategy_df = _n_leg_capital_loop(
        signal_raw      = signal_raw,
        weights_df      = weights_td,
        pnl_df          = pnl_df,
        held_prices_df  = prices_td,
        multipliers     = mults,
        t_costs         = tcs,
        initial_capital = initial_capital,
        roll_day_flag   = roll_flag,
        vol_window      = vol_window,
        vol_target_ann  = vol_target_ann,
    )

    # ── Attach signal diagnostics ──
    for col in ["residual", "zscore", "resid_mean", "resid_std", "upper_band", "lower_band"]:
        if col in signal_df.columns:
            strategy_df[col] = signal_df[col].reindex(trade_idx)

    return {
        "strategy_df":  strategy_df,
        "signal_df":    signal_df,
        "weights_df":   weights_df,
        "residuals":    residuals,
        "leg_dfs":      leg_dfs,
        "held_prices":  held_prices_df,
    }

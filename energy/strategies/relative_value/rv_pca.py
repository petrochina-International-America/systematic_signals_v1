"""
rv_pca.py
---------
PCA-based relative-value strategy for energy spreads.

Decomposes N leg prices into principal components (PCs) and constructs
a synthetic basket whose return is orthogonal to the first K systematic
factors — isolating the idiosyncratic / structural component.

Intuition for energy curves
---------------------------
PC1 = parallel price shift  (non-tradeable market beta)
PC2 = slope                 (near vs far — curve steepener / flattener)
PC3 = curvature             (butterfly)

Trading PC2 (default) gives a position that is orthogonal to the parallel
shift, with pure slope exposure.  Trading PC3 gives a butterfly.  Trading
the last PC gives the most idiosyncratic / mean-reverting component.

Weight construction
-------------------
1. Rolling covariance Σ of daily price changes over `lookback` days,
   estimated on strictly prior data (no lookahead).
2. Eigendecompose Σ (eigenvalues sorted descending).
3. Portfolio weights W = v_k (the k-th eigenvector), normalised so Σ|Wᵢ|=1.
4. Basket price  B_t = Σᵢ Wᵢ · Pᵢ(t)
5. Entry signal  = z-score of B_t vs its rolling distribution.

Sign convention: the component with the largest absolute weight is always
signed positive, giving a consistent long/short interpretation.

Public API
----------
rv_pca(legs, prices_list, expiry_list, ...)  ->  dict
    keys: strategy_df, signal_df, weights_df, basket, leg_dfs, held_prices

Shared utilities imported from rv_regression
--------------------------------------------
_n_leg_capital_loop, _build_residual_signal, _build_held_prices, _union_roll_flag
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from energy.accounting.contract_specs import CONTRACT_SPECS
from energy.strategies.relative_value.rv_zscore import _prepare_leg
from energy.strategies.relative_value.rv_regression import (
    _n_leg_capital_loop,
    _build_residual_signal,
    _build_held_prices,
    _union_roll_flag,
)


# ============================================================
# ROLLING PCA WEIGHTS
# ============================================================
def _rolling_pca_weights(
    prices_df: pd.DataFrame,
    lookback: int,
    pc_index: int = 2,
) -> tuple[pd.DataFrame, pd.Series]:
    """
    Rolling PCA on daily price changes, returning the pc_index-th eigenvector
    as portfolio weights at each point in time.

    Covariance is estimated on [t−lookback, t−1] (strictly prior — no lookahead).
    The k-th eigenvector (k = pc_index, 1-based) corresponds to the k-th largest
    eigenvalue of the covariance matrix of price changes.

    Sign convention: the element with the largest absolute value is always
    signed positive, so the "dominant leg" is consistently long.

    Parameters
    ----------
    pc_index : 1 = largest PC (parallel shift), 2 = slope, N = most idiosyncratic

    Returns
    -------
    weights_df : (n × n_legs) normalised weights (Σ|Wᵢ|=1), columns = leg names
    basket     : basket price B_t = Σᵢ Wᵢ(t−1) · Pᵢ(t), using prior-day weights
                 (strictly out-of-sample — no contemporaneous weight in signal)
    """
    cols   = prices_df.columns.tolist()
    n_legs = len(cols)
    n      = len(prices_df)

    if pc_index < 1 or pc_index > n_legs:
        raise ValueError(
            f"pc_index must be between 1 and {n_legs} (number of legs); got {pc_index}."
        )

    k = pc_index - 1  # 0-based index into descending-eigenvalue order

    P  = prices_df.to_numpy(dtype=float)   # (n, n_legs)
    dP = np.diff(P, axis=0)                # (n-1, n_legs) — daily changes

    W_out  = np.full((n, n_legs), np.nan)
    basket = np.full(n, np.nan)

    for t in range(lookback, n):
        sl   = slice(t - lookback, t)   # [t-lookback, t-1] in dP index
        dP_w = dP[sl]                   # lookback rows, n_legs cols

        if np.any(np.isnan(dP_w)):
            continue

        try:
            cov  = np.cov(dP_w, rowvar=False)           # (n_legs, n_legs)
            vals, vecs = np.linalg.eigh(cov)            # ascending eigenvalues
            order = np.argsort(vals)[::-1]              # descending order
            vecs  = vecs[:, order]
            v_k   = vecs[:, k]                          # k-th eigenvector
        except np.linalg.LinAlgError:
            continue

        # Anchor sign: largest-magnitude component is positive
        dominant = int(np.argmax(np.abs(v_k)))
        if v_k[dominant] < 0:
            v_k = -v_k

        norm = float(np.sum(np.abs(v_k)))
        if norm < 1e-12:
            continue

        w = v_k / norm
        W_out[t] = w

        # Basket uses yesterday's weights applied to today's prices (no lookahead)
        basket[t] = float(P[t] @ w)

    weights_df = pd.DataFrame(W_out, index=prices_df.index, columns=cols)
    basket_s   = pd.Series(basket, index=prices_df.index, name="basket_price")
    return weights_df, basket_s


# ============================================================
# PUBLIC API
# ============================================================
def rv_pca(
    legs: list[str],
    prices_list: list[pd.DataFrame],
    expiry_list: list[pd.Series],
    initial_capital: float = 1_000_000.0,
    lookback: int = 60,
    pc_index: int = 2,
    zscore_threshold: float = 1.0,
    vol_window: int = 0,
    vol_target_ann: float = 0.15,
    trade_start: str | None = None,
    roll_config: str = "prompt_EOM_roll",
) -> dict:
    """
    PCA-based N-leg RV strategy.

    Constructs a delta-neutral basket by projecting leg prices onto the
    pc_index-th principal component of daily price changes.  The basket
    is mean-reverting when the corresponding factor is idiosyncratic.

    Common use cases
    ----------------
    pc_index=2  Slope trade (orthogonal to parallel shift) — most liquid
    pc_index=3  Curvature / butterfly
    pc_index=N  Most idiosyncratic factor (smallest eigenvalue)

    Parameters
    ----------
    legs             : commodity names, e.g. ["WTI", "Brent", "RBOB"]
                       — must have matching CONTRACT_SPECS entries
    prices_list      : raw price DataFrames, same order as legs
    expiry_list      : expiry Series, same order as legs
    lookback         : rolling covariance estimation window
    pc_index         : which principal component to trade (1-indexed)
    zscore_threshold : z-score entry threshold for the basket
    vol_window       : trailing window for vol targeting on the basket's own
                       returns, position-independent (0 = off)
    vol_target_ann   : annualised vol target when vol_window > 0
    trade_start      : ISO date — equity index = 1.0 at this date
    roll_config      : CONTRACT_SPECS key for roll path construction

    Returns
    -------
    dict
        strategy_df : daily strategy with contracts, PnL, capital, equity_index
        signal_df   : basket_price, basket_mean/std, zscore, bands, signal_raw
        weights_df  : normalised PCA weights on the full intersection calendar
        basket      : synthetic basket price Series
        leg_dfs     : roll path DataFrames per leg (same order as legs)
        held_prices : DataFrame of held prices on the clean intersection calendar

    Example — 3-leg slope trade (WTI, Brent, RBOB)
    -----------------------------------------------
    pack = rv_pca(
        legs         = ["WTI", "Brent", "RBOB"],
        prices_list  = [wti_px, brent_px, rbob_px],
        expiry_list  = [wti_exp, brent_exp, rbob_exp],
        lookback     = 120,
        pc_index     = 2,
        zscore_threshold = 1.5,
        vol_window   = 20,
        roll_config  = "prompt_EOM_roll",
    )
    pack["strategy_df"]["equity_index"].plot()
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
    idx = held_prices_df.index

    # ── Contract specs ──
    specs = [CONTRACT_SPECS.get(n, {}) for n in legs]
    mults = np.array([float(s.get("contract_multiplier", 1)) for s in specs])
    tcs   = np.array([float(s.get("t_cost_abs", 0.0)) for s in specs])

    # ── Rolling PCA → weights + basket ──
    weights_df, basket = _rolling_pca_weights(held_prices_df, lookback, pc_index=pc_index)

    # ── Signal on basket ──
    signal_df = _build_residual_signal(basket, lookback, zscore_threshold)
    # Rename generic columns for clarity
    signal_df = signal_df.rename(columns={
        "residual":   "basket_price",
        "resid_mean": "basket_mean",
        "resid_std":  "basket_std",
    })

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
    for col in ["basket_price", "basket_mean", "basket_std",
                "zscore", "upper_band", "lower_band"]:
        if col in signal_df.columns:
            strategy_df[col] = signal_df[col].reindex(trade_idx)

    return {
        "strategy_df": strategy_df,
        "signal_df":   signal_df,
        "weights_df":  weights_df,
        "basket":      basket,
        "leg_dfs":     leg_dfs,
        "held_prices": held_prices_df,
    }

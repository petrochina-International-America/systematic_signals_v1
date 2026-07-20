import warnings

import pandas as pd
import numpy as np


def _build_value_signal(
    prices: pd.DataFrame,
    front_col: str = "F1",
    fair_value_col: str | None = None,
    value_score: pd.Series | None = None,
    epsilon: float = 0.0,
    fair_value_window: int | None = None,
    use_expanding_until_full_window: bool = True,
    normalize_score: bool = False,
) -> tuple[pd.Series, pd.Timestamp]:
    """
    Build the raw value signal and return:
        signal_raw : {-1,0,+1} signal at date t
        start_date : first date strategy is allowed to trade

    Value can be defined in one of three ways:
    1. precomputed `value_score`
    2. explicit `fair_value_col`
    3. internally estimated fair value from `front_col` using:
       - expanding mean until full window exists
       - then rolling mean thereafter

    Parameters
    ----------
    fair_value_window
        Number of observations in the fair value lookback.
        If None, no internal rolling fair value is built.
    use_expanding_until_full_window
        If True, use all available history before the full window is available.
    normalize_score
        If True, convert score to:
            (fair_value - price) / abs(fair_value)
        Otherwise use:
            fair_value - price
    """
    df = prices.copy()
    px = df[front_col].astype(float)

    if value_score is not None:
        score = value_score.reindex(df.index).astype(float)

    elif fair_value_col is not None:
        fv = df[fair_value_col].astype(float)

        if normalize_score:
            denom = fv.abs().replace(0, np.nan)
            score = (fv - px) / denom
        else:
            score = fv - px

    elif fair_value_window is not None:
        if use_expanding_until_full_window:
            obs_count = px.expanding(min_periods=1).count()
            expanding_fv = px.expanding(min_periods=1).mean()
            rolling_fv = px.rolling(
                window=fair_value_window,
                min_periods=fair_value_window,
            ).mean()

            fv = pd.Series(
                np.where(obs_count < fair_value_window, expanding_fv, rolling_fv),
                index=px.index,
                dtype=float,
            )
        else:
            fv = px.rolling(
                window=fair_value_window,
                min_periods=fair_value_window,
            ).mean()

        if normalize_score:
            denom = fv.abs().replace(0, np.nan)
            score = (fv - px) / denom
        else:
            score = fv - px

    else:
        raise ValueError(
            "Provide one of: value_score, fair_value_col, or fair_value_window"
        )

    signal_raw = pd.Series(
        np.where(score > epsilon, 1.0, np.where(score < -epsilon, -1.0, 0.0)),
        index=df.index,
        name="signal_raw",
        dtype=float,
    )

    valid_mask = score.notna()
    if valid_mask.any():
        start_date = df.index[valid_mask.argmax()]
    else:
        start_date = df.index[0]

    return signal_raw, start_date

def legacy_capstone_value(
    prices: pd.DataFrame,
    rolled_df: pd.DataFrame,
    front_col: str = "F1",
    fair_value_col: str | None = None,
    value_score: pd.Series | None = None,
    t_cost: float = 0.00,
    pct_t_cost: float | None = None,
    epsilon: float = 0.0,
) -> pd.DataFrame:
    """
    Legacy capstone value.

    Keeps the original capstone accounting style:
    - signal generated from value model
    - execution lagged via signal.shift(1)
    - PnL/costs handled in quote / normalized units
    """
    warnings.warn(
        "legacy_capstone_value books signal_lag[t] * daily_pnl[t] — the "
        "same-close convention retired on 2026-07-09. Historical reference "
        "only; use value() + build_measures (capital-loop timing).",
        DeprecationWarning, stacklevel=2,
    )
    signal_raw, start_date = _build_value_signal(
        prices=prices,
        front_col=front_col,
        fair_value_col=fair_value_col,
        value_score=value_score,
        epsilon=epsilon,
    )

    pnl_df = rolled_df[["daily_pnl", "t_cost", "roll_day_flag"]].copy()

    pnl_df["signal"] = signal_raw.reindex(pnl_df.index).fillna(0.0)
    pnl_df["signal_lag"] = pnl_df["signal"].shift(1).fillna(0.0)

    pnl_df.loc[pnl_df.index < start_date, ["signal", "signal_lag"]] = 0.0
    pnl_df.loc[pnl_df.index < start_date, "roll_day_flag"] = 0

    pnl_df["value_raw"] = pnl_df["signal_lag"] * pnl_df["daily_pnl"]

    delta = (pnl_df["signal"] - pnl_df["signal_lag"]).abs().fillna(0)
    pnl_df["sig_cost_mult"] = np.select(
        [delta == 0, delta == 1, delta == 2],
        [0, 1, 2],
        default=0,
    )

    roll_mult = pnl_df["roll_day_flag"] * 2
    combined = np.maximum(pnl_df["sig_cost_mult"], roll_mult)

    flat_overlap = (
        (pnl_df["roll_day_flag"] == 1)
        & (pnl_df["signal_lag"] != 0)
        & (pnl_df["signal"] == 0)
    )
    combined[flat_overlap] = 1

    pnl_df["total_cost_mult"] = combined

    if not pnl_df.empty:
        i0, i1 = pnl_df.index[0], pnl_df.index[-1]
        pnl_df.loc[i0, "total_cost_mult"] = max(pnl_df.loc[i0, "total_cost_mult"], 1)
        pnl_df.loc[i1, "total_cost_mult"] = max(pnl_df.loc[i1, "total_cost_mult"], 1)

    pnl_df["total_cost_mult"] = pnl_df["total_cost_mult"].clip(upper=2)

    norm_scale = prices.attrs.get("norm_scale", 1.0)
    px_for_cost = prices[front_col].reindex(pnl_df.index).astype(float)

    if pct_t_cost is not None and pct_t_cost > 0:
        base_cost = pct_t_cost * px_for_cost.abs()
    else:
        abs_tc = t_cost * norm_scale
        base_cost = pd.Series(abs_tc, index=pnl_df.index, dtype=float)

    sig_cost = pnl_df["total_cost_mult"] * base_cost

    roll_t_cost = rolled_df["t_cost"].reindex(pnl_df.index).fillna(0.0)
    roll_cost = -roll_t_cost

    total_cost = sig_cost + roll_cost

    pnl_df["t_cost"] = total_cost
    pnl_df.loc[pnl_df.index < start_date, "t_cost"] = 0.0

    pnl_df["trade_count"] = pnl_df["total_cost_mult"]

    pnl_df["net_pnl"] = pnl_df["value_raw"] - pnl_df["t_cost"]
    pnl_df["equity_line"] = pnl_df["net_pnl"].cumsum()

    out = pnl_df.rename(columns={"value_raw": "daily_pnl", "roll_day_flag": "roll_flag"})
    out["signal"] = out["signal"].astype(float)

    return out[
        [
            "daily_pnl",
            "t_cost",
            "net_pnl",
            "roll_flag",
            "equity_line",
            "trade_count",
            "signal",
        ]
    ]

def value(
    prices: pd.DataFrame,
    rolled_df: pd.DataFrame,
    front_col: str = "F1",
    fair_value_col: str | None = None,
    value_score: pd.Series | None = None,
    epsilon: float = 0.0,
    fair_value_window: int | None = None,
    use_expanding_until_full_window: bool = True,
    normalize_score: bool = False,
) -> pd.DataFrame:
    """
    MTM-ready value path.
    """
    signal_raw, start_date = _build_value_signal(
        prices=prices,
        front_col=front_col,
        fair_value_col=fair_value_col,
        value_score=value_score,
        epsilon=epsilon,
        fair_value_window=fair_value_window,
        use_expanding_until_full_window=use_expanding_until_full_window,
        normalize_score=normalize_score,
    )

    out = rolled_df[["daily_pnl", "held_contract", "roll_day_flag"]].copy()

    out["signal_raw"] = signal_raw.reindex(out.index).fillna(0.0)
    out["position"] = out["signal_raw"].shift(1).fillna(0.0)

    out.loc[out.index < start_date, ["signal_raw", "position"]] = 0.0
    out.loc[out.index < start_date, "roll_day_flag"] = 0

    prev_position = out["position"].shift(1).fillna(0.0)
    position_change = (out["position"] != prev_position).astype(int)
    live_roll = ((out["roll_day_flag"] == 1) & (out["position"] != 0)).astype(int)

    out["rebalance_flag"] = np.maximum(position_change, live_roll)

    if not out.empty:
        out.iloc[0, out.columns.get_loc("rebalance_flag")] = 1

    out = out.rename(columns={"roll_day_flag": "roll_flag"})

    return out[
        [
            "daily_pnl",
            "held_contract",
            "roll_flag",
            "signal_raw",
            "position",
            "rebalance_flag",
        ]
    ]
import warnings

import pandas as pd
import numpy as np


def _build_carry_signal(
    prices: pd.DataFrame,
    front_col: str = "F1",
    end_col: str = "F4",
    epsilon: float = 0.0,
    epsilon_pct: float = 0.0,
) -> tuple[pd.Series, pd.Timestamp, pd.Series]:
    """
    Build the raw carry signal and return:
        signal_raw : {-1,0,+1} signal at date t
        start_date : first date strategy is allowed to trade
        raw_slope  : continuous curve slope (front − end) before discretization

    epsilon_pct overrides epsilon when > 0: the dead-zone is computed as
    epsilon_pct / 100 × rolling_mean(|spread|, 60d), so the buffer adapts
    to the typical spread magnitude without circular dependency on the
    current spread value.
    """
    df = prices.copy()

    diff = df[front_col].astype(float) - df[end_col].astype(float)

    if epsilon_pct > 0:
        rolling_avg_spread = diff.abs().rolling(window=60, min_periods=10).mean()
        eps = (epsilon_pct / 100.0) * rolling_avg_spread
        eps = eps.fillna(0.0)
    else:
        eps = epsilon

    signal_raw = pd.Series(
        np.where(diff > eps, 1.0, np.where(diff < -eps, -1.0, 0.0)),
        index=df.index,
        name="signal_raw",
        dtype=float,
    )

    valid_mask = df[[front_col, end_col]].notna().all(axis=1)
    if valid_mask.any():
        start_date = df.index[valid_mask.argmax()]
    else:
        start_date = df.index[0]

    return signal_raw, start_date, diff


def legacy_capstone_carry(
    prices: pd.DataFrame,
    rolled_df: pd.DataFrame,
    front_col: str = "F1",
    end_col: str = "F4",
    t_cost: float = 0.00,              # absolute cost in ORIGINAL quote units
    pct_t_cost: float | None = None,   # fraction of PRICE, e.g. 0.001 = 10 bps
    epsilon: float = 0.0,
) -> pd.DataFrame:
    """
    Legacy capstone carry.

    Keeps the original behavior:
    - signal is generated from curve slope
    - execution is lagged via signal.shift(1)
    - PnL and transaction costs are handled in quote / normalized units
    - outputs legacy daily_pnl / net_pnl / equity_line
    """
    warnings.warn(
        "legacy_capstone_carry books signal_lag[t] * daily_pnl[t] — the "
        "same-close convention retired on 2026-07-09. Historical reference "
        "only; use carry() + build_measures (capital-loop timing).",
        DeprecationWarning, stacklevel=2,
    )
    signal_raw, start_date, _raw_slope = _build_carry_signal(
        prices=prices,
        front_col=front_col,
        end_col=end_col,
        epsilon=epsilon,
    )

    pnl_df = rolled_df[["daily_pnl", "t_cost", "roll_day_flag"]].copy()

    pnl_df["signal"] = signal_raw.reindex(pnl_df.index).fillna(0.0)
    pnl_df["signal_lag"] = pnl_df["signal"].shift(1).fillna(0.0)

    pnl_df.loc[pnl_df.index < start_date, ["signal", "signal_lag"]] = 0.0
    pnl_df.loc[pnl_df.index < start_date, "roll_day_flag"] = 0

    # raw carry PnL: held position times underlying rolled PnL
    pnl_df["carry_raw"] = pnl_df["signal_lag"] * pnl_df["daily_pnl"]

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

    pnl_df["net_pnl"] = pnl_df["carry_raw"] - pnl_df["t_cost"]
    pnl_df["equity_line"] = pnl_df["net_pnl"].cumsum()

    out = pnl_df.rename(columns={"carry_raw": "daily_pnl", "roll_day_flag": "roll_flag"})
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


def spread_carry(
    prices: pd.DataFrame,
    spread_rolled_df: pd.DataFrame,
    front_col: str = "F1",
    end_col: str = "F4",
    epsilon: float = 0.0,
) -> pd.DataFrame:
    """
    Carry strategy applied to a continuously-rolled time spread.

    Signal is identical to single-leg carry: derived from the curve slope
    (front_col - end_col in prices). No reinvention — reuses _build_carry_signal.

    Position logic:
        signal = +1 (backwardation): long spread (long near, short far)
            — prompt spread pays the richest carry when curve is inverted
        signal = -1 (contango): short spread (short near, long far)
            — collect contango carry by reversing; near leg bleeds into roll

    The daily_pnl from roll_time_spread is already "1 unit long spread" PnL.
    build_measures applies the position signal to sign it, so no extra logic needed here.

    Parameters
    ----------
    prices : DataFrame
        Full price DataFrame (F1, F2, ...) — used for signal generation only.
    spread_rolled_df : DataFrame
        Output of roll_time_spread() — must contain daily_pnl, roll_day_flag, t_cost.
    front_col : str
        Near tenor for carry signal (default F1 — prompt month).
    end_col : str
        Deferred tenor for carry signal (default F4).
    epsilon : float
        Dead-band around zero: |diff| <= epsilon → signal = 0 (flat).

    Returns
    -------
    DataFrame with columns matching carry() output:
        daily_pnl      : raw spread PnL per unit (unsigned direction — 1 = long spread)
        t_cost         : roll t_cost from spread path (both legs included)
        roll_flag      : roll_day_flag from spread path
        signal_raw     : {-1, 0, +1} carry signal on date t
        position       : signal_raw.shift(1) — actual held direction
        rebalance_flag : 1 on position changes or live-position roll days
    """
    signal_raw, start_date, _raw_slope = _build_carry_signal(
        prices=prices,
        front_col=front_col,
        end_col=end_col,
        epsilon=epsilon,
    )

    out = spread_rolled_df[["daily_pnl", "t_cost", "roll_day_flag"]].copy()

    out["signal_raw"] = signal_raw.reindex(out.index).fillna(0.0)
    out["position"]   = out["signal_raw"].shift(1).fillna(0.0)

    out.loc[out.index < start_date, ["signal_raw", "position"]] = 0.0
    out.loc[out.index < start_date, "roll_day_flag"] = 0

    prev_position   = out["position"].shift(1).fillna(0.0)
    position_change = (out["position"] != prev_position).astype(int)
    live_roll       = ((out["roll_day_flag"] == 1) & (out["position"] != 0)).astype(int)

    out["rebalance_flag"] = np.maximum(position_change, live_roll)

    if not out.empty:
        out.iloc[0, out.columns.get_loc("rebalance_flag")] = 1

    out = out.rename(columns={"roll_day_flag": "roll_flag"})

    return out[["daily_pnl", "t_cost", "roll_flag", "signal_raw", "position", "rebalance_flag"]]


def carry(
    prices: pd.DataFrame,
    rolled_df: pd.DataFrame,
    front_col: str = "F1",
    end_col: str = "F4",
    epsilon: float = 0.0,
    epsilon_pct: float = 0.0,
) -> pd.DataFrame:
    """
    MTM-ready carry path.

    Outputs a path for the MTM engine rather than a fully-accounted legacy equity line.

    Conventions:
    - signal_raw : signal decided on date t from indicators
    - position   : actual held position used for date t exposure/PnL
                   (already lagged, so no extra lag should be applied in MTM)
    - daily_pnl  : raw 1-contract underlying rolled PnL (UNSIGNED)
    - rebalance_flag : 1 when account should resize due to:
          * position change
          * roll while position is live
    """
    signal_raw, start_date, _raw_slope = _build_carry_signal(
        prices=prices,
        front_col=front_col,
        end_col=end_col,
        epsilon=epsilon,
        epsilon_pct=epsilon_pct,
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


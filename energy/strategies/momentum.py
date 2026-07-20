import warnings

import pandas as pd
import numpy as np


def _build_momentum_signal(
    prices: pd.DataFrame,
    front_col: str = "F1",
    short_ma: int = 1,
    long_ma: int = 20,
    epsilon: float = 0.0,
    ma_pairs=None,
    weights=None,
) -> tuple[pd.Series, pd.Timestamp, pd.Series]:
    """
    Build the raw momentum signal and return:
        signal_raw : {-1,0,+1} signal at date t
        start_date : first date strategy is allowed to trade
        raw_score  : continuous signal strength before discretization
    """
    df = prices.copy()
    px = df[front_col].astype(float)

    if ma_pairs is None:
        short_avg = px.rolling(window=short_ma, min_periods=short_ma).mean()
        long_avg = px.rolling(window=long_ma, min_periods=long_ma).mean()
        raw_score = short_avg - long_avg

        start_date = df.index[long_ma - 1] if len(df) >= long_ma else df.index[0]
    else:
        ma_pairs = list(ma_pairs)

        if weights is None:
            weights = np.ones(len(ma_pairs), dtype=float)
        else:
            weights = np.asarray(weights, dtype=float)
            if len(weights) != len(ma_pairs):
                raise ValueError("weights must have same length as ma_pairs")

        weights = weights / weights.sum()

        score_cols = []
        max_L = 0

        for (s, l) in ma_pairs:
            if s <= 0 or l <= 0 or s >= l:
                raise ValueError(
                    f"Invalid MA pair (short={s}, long={l}); require 0 < short < long"
                )

            max_L = max(max_L, l)

            ma_s = px.rolling(window=s, min_periods=s).mean()
            ma_l = px.rolling(window=l, min_periods=l).mean()
            score_cols.append(ma_s / ma_l - 1.0)

        score_df = pd.concat(score_cols, axis=1)

        # Re-normalize weights per day so warmup NaNs don't dilute the signal.
        # On any given day, only the pairs with enough history contribute;
        # their weights are scaled to sum to 1.0 so the signal stays at full scale.
        w_arr = np.broadcast_to(weights, score_df.shape).copy()
        w_arr[score_df.isna().values] = 0.0
        w_sum = w_arr.sum(axis=1, keepdims=True)
        w_norm = np.where(w_sum > 0, w_arr / w_sum, 0.0)
        raw_score = pd.Series(
            np.nansum(score_df.values * w_norm, axis=1),
            index=score_df.index,
        )

        start_date = df.index[max_L - 1] if len(df) >= max_L else df.index[0]

    signal_raw = pd.Series(
        np.where(raw_score > epsilon, 1.0, np.where(raw_score < -epsilon, -1.0, 0.0)),
        index=raw_score.index,
        name="signal_raw",
        dtype=float,
    )

    return signal_raw, start_date, raw_score


def legacy_capstone_momentum(
    prices: pd.DataFrame,
    rolled_df: pd.DataFrame,
    front_col: str = "F1",
    short_ma: int = 1,
    long_ma: int = 20,
    t_cost: float = 0.00,       # absolute cost in ORIGINAL quote units
    pct_t_cost=None,            # if set, fraction of |daily_pnl|
    epsilon: float = 0.0,
    ma_pairs=None,
    weights=None,
    prices_signal: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    Legacy capstone momentum.

    Keeps the original behavior:
    - signal is generated from price MAs
    - execution is lagged via signal.shift(1)
    - PnL and transaction costs are handled in quote / normalized units
    - outputs legacy daily_pnl / net_pnl / equity_line

    prices_signal : optional separate DataFrame used ONLY for MA/signal
        computation. Pass full price history here (no date filter) so that
        MAs are warmed up before the first trade date. If omitted, `prices`
        is used for both signal and cost lookups — but a warning is raised
        when insufficient warmup is detected.
    """
    warnings.warn(
        "legacy_capstone_momentum books signal_lag[t] * daily_pnl[t] — the "
        "same-close convention retired on 2026-07-09. Historical reference "
        "only; use momentum() + build_measures (capital-loop timing).",
        DeprecationWarning, stacklevel=2,
    )
    sig_prices = prices_signal if prices_signal is not None else prices

    # Warmup guard: warn when prices don't extend before the roll-path start.
    max_window = long_ma if ma_pairs is None else max(l for _, l in ma_pairs)
    if not rolled_df.empty and not sig_prices.empty:
        trade_start = rolled_df.index[0]
        warmup_rows = (sig_prices.index < trade_start).sum()
        if warmup_rows < max_window:
            warnings.warn(
                f"momentum warmup bias: only {warmup_rows} rows of price history "
                f"exist before trade_start={trade_start.date()} but the longest MA "
                f"window is {max_window}. Pass `prices_signal=<full_price_history>` "
                f"to pre-warm the MAs before the live trading window.",
                stacklevel=2,
            )

    signal_raw, start_date, _raw_score = _build_momentum_signal(
        prices=sig_prices,
        front_col=front_col,
        short_ma=short_ma,
        long_ma=long_ma,
        epsilon=epsilon,
        ma_pairs=ma_pairs,
        weights=weights,
    )

    pnl_df = rolled_df[["daily_pnl", "t_cost", "roll_day_flag"]].copy()

    pnl_df["signal"] = signal_raw.reindex(pnl_df.index).fillna(0.0)
    pnl_df["signal_lag"] = pnl_df["signal"].shift(1).fillna(0.0)

    pnl_df.loc[pnl_df.index < start_date, ["signal", "signal_lag"]] = 0.0
    pnl_df.loc[pnl_df.index < start_date, "roll_day_flag"] = 0

    pnl_df["mom_raw"] = pnl_df["signal_lag"] * pnl_df["daily_pnl"]

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

    pnl_df["net_pnl"] = pnl_df["mom_raw"] - pnl_df["t_cost"]
    pnl_df["equity_line"] = pnl_df["net_pnl"].cumsum()

    out = pnl_df.rename(columns={"mom_raw": "daily_pnl", "roll_day_flag": "roll_flag"})
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


def momentum(
    prices: pd.DataFrame,
    rolled_df: pd.DataFrame,
    front_col: str = "F1",
    short_ma: int = 1,
    long_ma: int = 20,
    epsilon: float = 0.0,
    ma_pairs=None,
    weights=None,
    prices_signal: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    MTM-ready momentum path.

    Outputs a path for the MTM engine rather than a fully-accounted legacy equity line.

    Conventions:
    - signal_raw : signal decided on date t from indicators
    - position   : actual held position used for date t exposure/PnL
                   (already lagged, so no extra lag should be applied in MTM)
    - daily_pnl  : raw 1-contract underlying rolled PnL (UNSIGNED)
    - rebalance_flag : 1 when account should resize due to:
          * position change
          * roll while position is live

    prices_signal : optional separate DataFrame used ONLY for MA/signal
        computation. Pass the full-history CLEAN roll-continuous frame here
        (leg_signal_series as a 'SIGNAL' column, with front_col='SIGNAL') so
        the MAs are warmed up before the first trade date. If omitted, the
        signal input defaults to the roll-continuous series built from
        rolled_df's own flows (leg_signal_series) — NEVER the raw stitched
        front column: raw generics jump at every re-rank even when no price
        moved, which fires phantom MA crossings (Sharpe 0.282 stitched vs
        0.125 clean — 2026-07-14 signal-series audit; constant-price gate in
        tests/test_signal_series_integrity.py). A warning is still raised
        when the signal input has insufficient pre-trade warmup.
    """
    if prices_signal is not None:
        sig_prices = prices_signal
        sig_col = front_col
    else:
        # Default signal input: the sanctioned roll-continuous series (flow
        # cumsum anchored at the first held price), built from the roll path
        # itself. Spans rolled_df.index only, so MAs warm up in-window —
        # callers wanting pre-trade warmup pass the full-history clean frame.
        from energy.strategies.rolling import leg_signal_series
        sig_prices = pd.DataFrame({"SIGNAL": leg_signal_series(rolled_df, prices)})
        sig_col = "SIGNAL"

    # Warmup guard: warn when the signal input doesn't extend before the
    # roll-path start.
    max_window = long_ma if ma_pairs is None else max(l for _, l in ma_pairs)
    if not rolled_df.empty and not sig_prices.empty:
        trade_start = rolled_df.index[0]
        warmup_rows = (sig_prices.index < trade_start).sum()
        if warmup_rows < max_window:
            warnings.warn(
                f"momentum warmup bias: only {warmup_rows} rows of price history "
                f"exist before trade_start={trade_start.date()} but the longest MA "
                f"window is {max_window}. Pass `prices_signal=<full-history CLEAN "
                f"roll-continuous frame>` (leg_signal_series) to pre-warm the MAs "
                f"before the live trading window.",
                stacklevel=2,
            )

    signal_raw, start_date, _raw_score = _build_momentum_signal(
        prices=sig_prices,
        front_col=sig_col,
        short_ma=short_ma,
        long_ma=long_ma,
        epsilon=epsilon,
        ma_pairs=ma_pairs,
        weights=weights,
    )

    out = rolled_df[["daily_pnl", "held_contract", "roll_day_flag"]].copy()

    out["signal_raw"] = signal_raw.reindex(out.index).fillna(0.0)

    # Initialize position[0] from the last signal BEFORE the trade window so
    # that on day 1 the strategy is already positioned (not flat).  This only
    # has effect when prices_signal provides pre-trade history; if there is no
    # pre-trade history the init falls back to 0.
    if not out.empty:
        pre_trade = signal_raw[signal_raw.index < out.index[0]]
        init_position = float(pre_trade.iloc[-1]) if not pre_trade.empty else 0.0
    else:
        init_position = 0.0

    out["position"] = out["signal_raw"].shift(1).fillna(init_position)

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


def price_momentum(*args, **kwargs) -> pd.DataFrame:
    """
    Backward-compat alias to the new MTM-ready momentum path.
    """
    return momentum(*args, **kwargs)
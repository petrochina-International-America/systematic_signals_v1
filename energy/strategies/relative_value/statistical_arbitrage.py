import numpy as np
import pandas as pd

from energy.accounting.contract_specs import CONTRACT_SPECS
from energy.accounting.mtm import build_roll_path


# ============================================================
# ROLL CONFIG / PREP
# ============================================================
def _get_roll_config(commodity_name: str) -> dict:
    spec = CONTRACT_SPECS.get(commodity_name, {})
    cfg = spec.get("prompt_EOM_roll")

    if cfg is None:
        raise ValueError(f"{commodity_name}: no prompt_EOM_roll config in CONTRACT_SPECS")

    return cfg


def _build_leg_roll_path(
    commodity_name: str,
    prices: pd.DataFrame,
    expiry_calendar: pd.Series,
) -> pd.DataFrame:
    """
    Shared MTM roll builder.
    """
    cfg = _get_roll_config(commodity_name)

    rolled_df = build_roll_path(
        prices=prices,
        expiry_calendar=expiry_calendar,
        style=cfg["style"],
        front_col=cfg.get("front_col", "F1"),
        next_col=cfg.get("next_col", "F2"),
        third_col=cfg.get("third_col"),
        mid_col=cfg.get("mid_col"),
        far_col=cfg.get("far_col"),
        roll_window=cfg.get("roll_window", 5),
    ).copy()

    required = ["daily_pnl", "held_contract", "roll_day_flag"]
    missing = [c for c in required if c not in rolled_df.columns]
    if missing:
        raise ValueError(f"{commodity_name}: rolled path missing columns {missing}")

    return rolled_df


def _extract_held_price(
    prices: pd.DataFrame,
    held_contract: pd.Series,
) -> pd.Series:
    out = pd.Series(index=held_contract.index, dtype=float, name="held_price")

    idx = held_contract.index.intersection(prices.index)
    hc = held_contract.reindex(idx)

    for dt, contract_col in hc.items():
        if pd.isna(contract_col):
            out.loc[dt] = np.nan
        elif contract_col in prices.columns:
            out.loc[dt] = prices.loc[dt, contract_col]
        else:
            out.loc[dt] = np.nan

    return out.reindex(held_contract.index)


def _prepare_leg_for_stat_arb(
    commodity_name: str,
    prices: pd.DataFrame,
    expiry_calendar: pd.Series,
) -> pd.DataFrame:
    rolled_df = _build_leg_roll_path(
        commodity_name=commodity_name,
        prices=prices,
        expiry_calendar=expiry_calendar,
    )

    out = rolled_df.copy()
    out["held_price"] = _extract_held_price(prices, out["held_contract"])
    return out


# ============================================================
# LEGACY SIGNAL SPACE
# ============================================================
def _build_pair_spread_from_rolled_prices(
    leg1_df: pd.DataFrame,
    leg2_df: pd.DataFrame,
) -> pd.Series:
    """
    Pair spread on the intersection of both legs' trading calendars:
        spread = leg1 - leg2
    Using intersection avoids NaN contamination from mismatched roll dates,
    which would corrupt the rolling mean and kill the signal.
    """
    idx = leg1_df.index.intersection(leg2_df.index).sort_values()

    out = pd.DataFrame(index=idx)
    out["px1"] = leg1_df["held_price"].reindex(idx)
    out["px2"] = leg2_df["held_price"].reindex(idx)

    return (out["px1"] - out["px2"]).rename("spread")


def _build_deviation_band_signal(
    spread: pd.Series,
    lookback: int = 60,
    deviation_band_pct: float = 0.15,
) -> pd.DataFrame:
    """
    Stat-arb signal using % deviation from rolling mean.

    deviation_pct = (spread - rolling_mean) / |rolling_mean|

    Entry when spread deviates more than deviation_band_pct (e.g. 15%) from
    its X-day rolling mean. Exit when spread reverts back through the mean.

    Signal convention:
    +1 = long spread  = long leg1 / short leg2
    -1 = short spread = short leg1 / long leg2

    IMPORTANT:
    The signal at date t is built ONLY from info available through t-1.
    So there is no explicit signal_lag parameter.
    """
    s = pd.Series(spread).astype(float)

    spread_mean = s.rolling(window=lookback, min_periods=lookback).mean()
    deviation = s - spread_mean

    denom = spread_mean.abs().replace(0, np.nan)
    deviation_pct = deviation / denom

    upper_band = spread_mean + deviation_band_pct * spread_mean.abs()
    lower_band = spread_mean - deviation_band_pct * spread_mean.abs()

    signal_raw = pd.Series(index=s.index, dtype=float, name="signal_raw")

    state = 0.0

    for i, dt in enumerate(s.index):
        if i == 0:
            signal_raw.loc[dt] = 0.0
            continue

        prev_dt = s.index[i - 1]

        prev_spread = s.loc[prev_dt]
        prev_mean = spread_mean.loc[prev_dt]
        prev_dev_pct = deviation_pct.loc[prev_dt]

        if pd.isna(prev_spread) or pd.isna(prev_mean) or pd.isna(prev_dev_pct):
            signal_raw.loc[dt] = 0.0
            state = 0.0
            continue

        if state == 0.0:
            # Spread rich yesterday -> short spread today
            if prev_dev_pct >= deviation_band_pct:
                state = -1.0

            # Spread cheap yesterday -> long spread today
            elif prev_dev_pct <= -deviation_band_pct:
                state = 1.0

        elif state == 1.0:
            # Long spread until yesterday's spread reverted back to / through mean
            if prev_spread >= prev_mean:
                state = 0.0

        elif state == -1.0:
            # Short spread until yesterday's spread reverted back to / through mean
            if prev_spread <= prev_mean:
                state = 0.0

        signal_raw.loc[dt] = state

    out = pd.DataFrame(index=s.index)
    out["spread"] = s
    out["spread_mean"] = spread_mean
    out["deviation"] = deviation
    out["deviation_pct"] = deviation_pct
    out["upper_band"] = upper_band
    out["lower_band"] = lower_band
    out["signal_raw"] = signal_raw.fillna(0.0)

    valid_mask = spread_mean.notna()
    if valid_mask.any():
        out.attrs["start_date"] = out.index[np.argmax(valid_mask.to_numpy())]
    else:
        out.attrs["start_date"] = out.index[0]

    return out


# ============================================================
# PAIR PNL SPACE
# ============================================================
def _build_pair_rolled_pnl(
    leg1_df: pd.DataFrame,
    leg2_df: pd.DataFrame,
) -> pd.DataFrame:
    idx = leg1_df.index.intersection(leg2_df.index).sort_values()

    out = pd.DataFrame(index=idx)
    out["leg1_daily_pnl"] = leg1_df["daily_pnl"].reindex(idx).fillna(0.0)
    out["leg2_daily_pnl"] = leg2_df["daily_pnl"].reindex(idx).fillna(0.0)

    roll1 = leg1_df["roll_day_flag"].reindex(idx).fillna(0).astype(int)
    roll2 = leg2_df["roll_day_flag"].reindex(idx).fillna(0).astype(int)
    out["roll_day_flag"] = np.maximum(roll1, roll2)

    hc1 = leg1_df["held_contract"].reindex(idx).fillna("NA").astype(str)
    hc2 = leg2_df["held_contract"].reindex(idx).fillna("NA").astype(str)
    out["held_contract"] = hc1 + " | " + hc2

    return out


# ============================================================
# COSTS / REBALANCE / EXECUTION
# ============================================================
def _build_leg_weights(position: pd.Series) -> pd.DataFrame:
    """
    Legacy pair structure:
    +1 position => +0.5 leg1, -0.5 leg2
    -1 position => -0.5 leg1, +0.5 leg2
    """
    out = pd.DataFrame(index=position.index)
    out["leg1_weight"] = 0.5 * position.astype(float)
    out["leg2_weight"] = -0.5 * position.astype(float)
    return out


def _build_rebalance_flag(
    leg1_weight: pd.Series,
    leg2_weight: pd.Series,
    roll_day_flag: pd.Series,
) -> pd.Series:
    prev_w1 = leg1_weight.shift(1).fillna(0.0)
    prev_w2 = leg2_weight.shift(1).fillna(0.0)

    signal_change = ((leg1_weight != prev_w1) | (leg2_weight != prev_w2)).astype(int)
    live_roll = (
        (roll_day_flag.fillna(0).astype(int) == 1)
        & ((leg1_weight != 0.0) | (leg2_weight != 0.0))
    ).astype(int)

    out = np.maximum(signal_change, live_roll)
    out = pd.Series(out, index=leg1_weight.index, name="rebalance_flag")

    if len(out) > 0:
        out.iloc[0] = 1

    return out


def _compute_stat_arb_t_cost(
    leg1_weight: pd.Series,
    leg2_weight: pd.Series,
    roll_day_flag: pd.Series,
    leg1_name: str,
    leg2_name: str,
) -> pd.Series:
    """
    Keep t_cost naming consistent with the rest of your code.
    """
    tc1 = CONTRACT_SPECS.get(leg1_name, {}).get("t_cost_abs", 0.0)
    tc2 = CONTRACT_SPECS.get(leg2_name, {}).get("t_cost_abs", 0.0)

    dw1 = leg1_weight.diff().abs().fillna(leg1_weight.abs())
    dw2 = leg2_weight.diff().abs().fillna(leg2_weight.abs())

    signal_cost = dw1 * tc1 + dw2 * tc2

    live_roll = (
        (roll_day_flag.fillna(0).astype(int) == 1)
        & ((leg1_weight != 0.0) | (leg2_weight != 0.0))
    ).astype(float)

    roll_cost = live_roll * (0.5 * tc1 + 0.5 * tc2)

    return (signal_cost + roll_cost).rename("t_cost")


def _apply_stat_arb_mtm(
    signal_raw: pd.Series,
    pair_pnl_df: pd.DataFrame,
    t_cost: pd.Series | None = None,
    initial_capital: float = 1_000_000.0,
) -> pd.DataFrame:
    import warnings
    warnings.warn(
        "_apply_stat_arb_mtm books weight[t] * leg_pnl[t] — the same-close "
        "convention retired on 2026-07-09. Historical reference only; use "
        "_stat_arb_capital_loop (capital-loop timing).",
        DeprecationWarning, stacklevel=2,
    )
    idx = signal_raw.index.intersection(pair_pnl_df.index).sort_values()

    df = pd.DataFrame(index=idx)
    df["signal_raw"] = signal_raw.reindex(idx).fillna(0.0)

    weights = _build_leg_weights(df["signal_raw"])
    df["leg1_weight"] = weights["leg1_weight"]
    df["leg2_weight"] = weights["leg2_weight"]

    df["leg1_daily_pnl"] = pair_pnl_df["leg1_daily_pnl"].reindex(idx).fillna(0.0)
    df["leg2_daily_pnl"] = pair_pnl_df["leg2_daily_pnl"].reindex(idx).fillna(0.0)
    df["roll_day_flag"] = pair_pnl_df["roll_day_flag"].reindex(idx).fillna(0).astype(int)
    df["held_contract"] = pair_pnl_df["held_contract"].reindex(idx).ffill()

    df["leg1_pnl"] = df["leg1_weight"] * df["leg1_daily_pnl"]
    df["leg2_pnl"] = df["leg2_weight"] * df["leg2_daily_pnl"]
    df["gross_pnl"] = df["leg1_pnl"] + df["leg2_pnl"]

    df["t_cost"] = 0.0 if t_cost is None else pd.Series(t_cost).reindex(idx).fillna(0.0)
    df["net_pnl"] = df["gross_pnl"] - df["t_cost"]

    df["capital"] = initial_capital + df["net_pnl"].cumsum()
    prev_capital = df["capital"].shift(1).replace(0, np.nan)
    df["daily_ret"] = (df["net_pnl"] / prev_capital).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    df["equity_index"] = df["capital"] / initial_capital

    df["rebalance_flag"] = _build_rebalance_flag(
        leg1_weight=df["leg1_weight"],
        leg2_weight=df["leg2_weight"],
        roll_day_flag=df["roll_day_flag"],
    )

    return df


# ============================================================
# LEG ANALYTICS
# ============================================================
def _compute_perf_metrics_from_pnl(
    pnl: pd.Series,
    annualization: int = 252,
) -> dict:
    pnl = pd.Series(pnl).fillna(0.0).astype(float)

    total_pnl = pnl.sum()
    avg_daily = pnl.mean()
    daily_vol = pnl.std(ddof=0)

    ann_pnl = avg_daily * annualization
    ann_vol = daily_vol * np.sqrt(annualization)
    sharpe = np.nan if ann_vol == 0 else ann_pnl / ann_vol

    equity = pnl.cumsum()
    hwm = equity.cummax()
    drawdown = equity - hwm
    max_drawdown = drawdown.min()

    rod = np.nan
    if max_drawdown < 0:
        rod = total_pnl / abs(max_drawdown)

    hit_rate = (pnl > 0).mean()

    return {
        "Total PnL": total_pnl,
        "Annual PnL": ann_pnl,
        "Annualized Vol": ann_vol,
        "Sharpe": sharpe,
        "Max Drawdown": max_drawdown,
        "Return on Drawdown": rod,
        "Hit Rate": hit_rate,
    }


def stat_arb_leg_decomposition(
    strategy_df: pd.DataFrame,
    leg1_name: str,
    leg2_name: str,
) -> dict:
    contrib = pd.DataFrame(index=strategy_df.index)
    contrib[leg1_name] = strategy_df["leg1_pnl"].fillna(0.0)
    contrib[leg2_name] = strategy_df["leg2_pnl"].fillna(0.0)
    contrib["gross_pnl"] = contrib[leg1_name] + contrib[leg2_name]
    contrib["net_pnl"] = strategy_df["net_pnl"].fillna(0.0)

    cumulative = contrib.cumsum()

    metrics = pd.DataFrame({
        leg1_name: _compute_perf_metrics_from_pnl(contrib[leg1_name]),
        leg2_name: _compute_perf_metrics_from_pnl(contrib[leg2_name]),
        "gross_pnl": _compute_perf_metrics_from_pnl(contrib["gross_pnl"]),
        "net_pnl": _compute_perf_metrics_from_pnl(contrib["net_pnl"]),
    }).T

    return {
        "daily_pnl": contrib,
        "cumulative_pnl": cumulative,
        "metrics": metrics,
    }


# ============================================================
# MTM CAPITAL LOOP
# ============================================================
def _stat_arb_capital_loop(
    signal_raw: pd.Series,
    leg1_pnl_price: pd.Series,
    leg2_pnl_price: pd.Series,
    leg1_held_price: pd.Series,
    leg2_held_price: pd.Series,
    roll_day_flag: pd.Series,
    mult1: float,
    mult2: float,
    tc1: float,
    tc2: float,
    initial_capital: float,
    vol_window: int = 0,
    vol_target_ann: float = 0.15,
    vol_min_obs: int | None = None,
    vol_floor: float = 1e-8,
    vol_scalar_cap: float | None = None,
    warmup_returns: pd.Series | None = None,
) -> pd.DataFrame:
    """
    MTM capital loop for a stat-arb pair.

    At each rebalance, sizes both legs proportional to current capital:
        contracts1 = +0.5 * scalar * signal * capital / (px1 * mult1)
        contracts2 = -0.5 * scalar * signal * capital / (px2 * mult2)

    Vol scaling:
        scalar = (vol_target_ann / sqrt(252)) / realized_daily_vol_of_pair
    vol_scalar_cap bounds the scalar from above (None = uncapped). The
    uncapped loop raises leverage exactly when trailing spread vol is low —
    i.e. in chop — and has carried >9x into dislocation onsets; the cap is
    a hard ceiling on that mechanism, not a tuned parameter.
    where realized vol is measured on the pair's own price returns
    (0.5 * leg1_pnl / prev_px1 - 0.5 * leg2_pnl / prev_px2), independent of
    whether a position is open — NOT on the strategy's own equity returns,
    which are zero while flat and collapse the estimate over flat streaks.
    (mtm_from_path deliberately differs: it targets the strategy's own
    realized vol via self_vol_window.)
    Uses ONLY history available through t-1, so no look-ahead.
    Set vol_window=0 to disable.

    warmup_returns : pre-sample daily pair returns used to seed the vol
    estimator so the scalar is calibrated from day 0 instead of running the
    first vol_window days unscaled at 1.0 (the same convention
    build_measures uses for the single-leg strategies). MUST be in the same
    space as the in-sample estimator input: 0.5*leg1_flow/prev_px1 -
    0.5*leg2_flow/prev_px2, roll-aware flows — never stitched-level changes.
    """
    idx = signal_raw.index
    n = len(idx)

    if vol_min_obs is None:
        vol_min_obs = vol_window

    target_daily_vol = vol_target_ann / np.sqrt(252.0)

    sig      = signal_raw.to_numpy(dtype=float)
    pnl1     = leg1_pnl_price.reindex(idx).fillna(0.0).to_numpy(dtype=float)
    pnl2     = leg2_pnl_price.reindex(idx).fillna(0.0).to_numpy(dtype=float)
    px1      = leg1_held_price.reindex(idx).to_numpy(dtype=float)
    px2      = leg2_held_price.reindex(idx).to_numpy(dtype=float)
    roll     = roll_day_flag.reindex(idx).fillna(0).astype(int).to_numpy()

    # Rebalance flag: signal change or live roll while positioned
    w1 = 0.5 * sig
    w2 = -0.5 * sig
    prev_w1 = np.concatenate([[0.0], w1[:-1]])
    prev_w2 = np.concatenate([[0.0], w2[:-1]])
    sig_change = ((w1 != prev_w1) | (w2 != prev_w2)).astype(int)
    live_roll  = ((roll == 1) & ((w1 != 0.0) | (w2 != 0.0))).astype(int)
    reb        = np.maximum(sig_change, live_roll)
    reb[0]     = 1

    contracts1   = np.zeros(n, dtype=float)
    contracts2   = np.zeros(n, dtype=float)
    leg1_dollar  = np.zeros(n, dtype=float)
    leg2_dollar  = np.zeros(n, dtype=float)
    txn_cost     = np.zeros(n, dtype=float)
    capital      = np.full(n, np.nan, dtype=float)
    daily_ret    = np.full(n, np.nan, dtype=float)
    self_vol     = np.full(n, np.nan, dtype=float)
    vol_scalar   = np.ones(n, dtype=float)

    # Vol-estimator input: the pair's own daily return, position-independent.
    # 0.5 * (leg pnl / prev leg price) per leg is the fractional return the
    # account would earn per unit of vol scalar if fully invested, and it
    # stays defined while the strategy is flat.  Sizing on the strategy's own
    # equity returns (daily_ret) is forbidden here: flat streaks write exact
    # zeros into the window, collapse the vol estimate, and blow up position
    # size at re-entry.
    px1_prev = pd.Series(px1, dtype=float).ffill().shift(1).to_numpy()
    px2_prev = pd.Series(px2, dtype=float).ffill().shift(1).to_numpy()
    px1_prev = np.where(px1_prev > 0, px1_prev, np.nan)
    px2_prev = np.where(px2_prev > 0, px2_prev, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        pair_ret = 0.5 * pnl1 / px1_prev - 0.5 * pnl2 / px2_prev

    if warmup_returns is not None:
        warmup_arr = warmup_returns.dropna().to_numpy(dtype=float)
    else:
        warmup_arr = np.array([], dtype=float)

    def _scalar(t: int) -> float:
        if vol_window == 0:
            return 1.0
        hist = pair_ret[:t]
        valid_in = hist[~np.isnan(hist)]
        # seed from pre-sample history until the in-sample window fills
        # (same convention as build_measures for the single-leg strategies)
        need = vol_min_obs - len(valid_in)
        if need > 0 and len(warmup_arr) > 0:
            valid = np.concatenate([warmup_arr[-need:], valid_in])
        else:
            valid = valid_in
        if len(valid) < vol_min_obs:
            return 1.0
        realized = float(valid[-vol_window:].std(ddof=1))
        self_vol[t] = realized
        if np.isnan(realized) or realized < vol_floor:
            return 1.0
        sc = target_daily_vol / realized
        if vol_scalar_cap is not None:
            sc = min(sc, vol_scalar_cap)
        return sc

    def _safe_px(arr: np.ndarray, t: int) -> float:
        v = arr[t]
        if not np.isnan(v) and v > 0:
            return v
        # fall back to most recent valid price
        for k in range(t - 1, -1, -1):
            if not np.isnan(arr[k]) and arr[k] > 0:
                return arr[k]
        return np.nan

    # --- t=0 initial sizing (seeded from warmup vol when available, same
    # convention as build_measures' _warmup_vol_scalar) ---
    s0    = sig[0]
    if vol_window > 0 and len(warmup_arr) >= vol_min_obs:
        rv0 = float(warmup_arr[-vol_window:].std(ddof=1))
        if np.isnan(rv0) or rv0 < vol_floor:
            sc0 = 1.0
        else:
            sc0 = target_daily_vol / rv0
            if vol_scalar_cap is not None:
                sc0 = min(sc0, vol_scalar_cap)
            self_vol[0] = rv0
    else:
        sc0 = 1.0
    vol_scalar[0] = sc0
    px1_0 = _safe_px(px1, 0)
    px2_0 = _safe_px(px2, 0)

    if s0 != 0 and not np.isnan(px1_0) and not np.isnan(px2_0):
        n1_0 = 0.5 * sc0 * s0 * initial_capital / (px1_0 * mult1)
        n2_0 = -0.5 * sc0 * s0 * initial_capital / (px2_0 * mult2)
        entry_cost = abs(n1_0) * tc1 + abs(n2_0) * tc2
    else:
        n1_0 = n2_0 = entry_cost = 0.0

    contracts1[0] = n1_0
    contracts2[0] = n2_0
    txn_cost[0]   = entry_cost
    capital[0]    = initial_capital - entry_cost

    # --- daily loop ---
    for t in range(1, n):
        dp1 = contracts1[t - 1] * pnl1[t] * mult1
        dp2 = contracts2[t - 1] * pnl2[t] * mult2
        leg1_dollar[t] = dp1
        leg2_dollar[t] = dp2
        capital_pre    = capital[t - 1] + dp1 + dp2

        daily_ret[t] = (
            (dp1 + dp2) / capital[t - 1]
            if capital[t - 1] > 0
            else np.nan
        )

        if reb[t]:
            sc_t  = _scalar(t)
            vol_scalar[t] = sc_t
            px1_t = _safe_px(px1, t)
            px2_t = _safe_px(px2, t)
            s_t   = sig[t]

            if s_t != 0 and not np.isnan(px1_t) and not np.isnan(px2_t) and capital_pre > 0:
                new_n1 =  0.5 * sc_t * s_t * capital_pre / (px1_t * mult1)
                new_n2 = -0.5 * sc_t * s_t * capital_pre / (px2_t * mult2)
            else:
                new_n1 = new_n2 = 0.0

            cost_t = abs(new_n1 - contracts1[t - 1]) * tc1 + abs(new_n2 - contracts2[t - 1]) * tc2
            contracts1[t] = new_n1
            contracts2[t] = new_n2
            txn_cost[t]   = cost_t
            capital[t]    = capital_pre - cost_t
        else:
            vol_scalar[t]  = vol_scalar[t - 1]
            contracts1[t]  = contracts1[t - 1]
            contracts2[t]  = contracts2[t - 1]
            capital[t]     = capital_pre

    cap_s      = pd.Series(capital,     index=idx)
    gross_pnl  = pd.Series(leg1_dollar + leg2_dollar, index=idx)
    tc_s       = pd.Series(txn_cost,    index=idx)

    out = pd.DataFrame(index=idx)
    out["signal_raw"]       = sig
    out["leg1_contracts"]   = contracts1
    out["leg2_contracts"]   = contracts2
    out["leg1_dollar_pnl"]  = leg1_dollar
    out["leg2_dollar_pnl"]  = leg2_dollar
    out["gross_pnl"]        = gross_pnl
    out["t_cost"]           = tc_s
    out["net_pnl"]          = gross_pnl - tc_s
    out["capital"]          = cap_s
    out["daily_ret"]        = pd.Series(daily_ret, index=idx)
    out["equity_index"]     = cap_s / initial_capital
    out["rebalance_flag"]   = reb
    out["roll_day_flag"]    = roll
    out["self_vol"]         = pd.Series(self_vol,   index=idx)
    out["vol_scalar"]       = pd.Series(vol_scalar, index=idx)

    return out


def _stat_arb_leg_decomposition_mtm(
    strategy_df: pd.DataFrame,
    leg1_name: str,
    leg2_name: str,
) -> dict:
    contrib = pd.DataFrame(index=strategy_df.index)
    contrib[leg1_name]  = strategy_df["leg1_dollar_pnl"].fillna(0.0)
    contrib[leg2_name]  = strategy_df["leg2_dollar_pnl"].fillna(0.0)
    contrib["gross_pnl"] = contrib[leg1_name] + contrib[leg2_name]
    contrib["net_pnl"]   = strategy_df["net_pnl"].fillna(0.0)

    cumulative = contrib.cumsum()

    metrics = pd.DataFrame({
        leg1_name:   _compute_perf_metrics_from_pnl(contrib[leg1_name]),
        leg2_name:   _compute_perf_metrics_from_pnl(contrib[leg2_name]),
        "gross_pnl": _compute_perf_metrics_from_pnl(contrib["gross_pnl"]),
        "net_pnl":   _compute_perf_metrics_from_pnl(contrib["net_pnl"]),
    }).T

    return {
        "daily_pnl":      contrib,
        "cumulative_pnl": cumulative,
        "metrics":        metrics,
    }


def statistical_arbitrage(
    leg1_name: str,
    leg2_name: str,
    prices1: pd.DataFrame,
    prices2: pd.DataFrame,
    expiry1: pd.Series,
    expiry2: pd.Series,
    initial_capital: float = 1_000_000.0,
    lookback: int = 60,
    deviation_band_pct: float = 0.10,
    vol_window: int = 0,
    vol_target_ann: float = 0.15,
) -> dict:
    """
    MTM stat-arb.

    Deviation-band signal on price spread, capital-proportional contract sizing,
    and optional vol targeting:
        scalar = (vol_target_ann / sqrt(252)) / trailing_realized_daily_vol_of_pair
    Realized vol is measured on the pair's own price returns, independent of
    whether a position is open (see _stat_arb_capital_loop).
    Signal is pure direction (±1/0); vol scalar lives in the sizing layer only.
    Set vol_window=0 to disable vol scaling.
    """
    leg1_df = _prepare_leg_for_stat_arb(leg1_name, prices1, expiry1)
    leg2_df = _prepare_leg_for_stat_arb(leg2_name, prices2, expiry2)

    spread    = _build_pair_spread_from_rolled_prices(leg1_df, leg2_df)
    signal_df = _build_deviation_band_signal(spread, lookback, deviation_band_pct)
    pair_pnl  = _build_pair_rolled_pnl(leg1_df, leg2_df)

    spec1  = CONTRACT_SPECS.get(leg1_name, {})
    spec2  = CONTRACT_SPECS.get(leg2_name, {})
    mult1  = float(spec1.get("contract_multiplier", 1))
    mult2  = float(spec2.get("contract_multiplier", 1))
    tc1    = float(spec1.get("t_cost_abs", 0.0))
    tc2    = float(spec2.get("t_cost_abs", 0.0))

    idx        = signal_df.index.intersection(pair_pnl.index).sort_values()
    signal_raw = signal_df["signal_raw"].reindex(idx).fillna(0.0)

    strategy_df = _stat_arb_capital_loop(
        signal_raw      = signal_raw,
        leg1_pnl_price  = pair_pnl["leg1_daily_pnl"].reindex(idx).fillna(0.0),
        leg2_pnl_price  = pair_pnl["leg2_daily_pnl"].reindex(idx).fillna(0.0),
        leg1_held_price = leg1_df["held_price"].reindex(idx),
        leg2_held_price = leg2_df["held_price"].reindex(idx),
        roll_day_flag   = pair_pnl["roll_day_flag"].reindex(idx).fillna(0),
        mult1           = mult1,
        mult2           = mult2,
        tc1             = tc1,
        tc2             = tc2,
        initial_capital = initial_capital,
        vol_window      = vol_window,
        vol_target_ann  = vol_target_ann,
    )

    for col in ["spread", "spread_mean", "deviation", "deviation_pct", "upper_band", "lower_band"]:
        if col in signal_df.columns:
            strategy_df[col] = signal_df[col].reindex(idx)

    strategy_df["held_contract"] = pair_pnl["held_contract"].reindex(idx)

    leg_pack = _stat_arb_leg_decomposition_mtm(strategy_df, leg1_name, leg2_name)

    return {
        "strategy_df":        strategy_df,
        "signal_df":          signal_df,
        "spread":             spread,
        "leg1_df":            leg1_df,
        "leg2_df":            leg2_df,
        "pair_pnl_df":        pair_pnl,
        "leg_daily_pnl":      leg_pack["daily_pnl"],
        "leg_cumulative_pnl": leg_pack["cumulative_pnl"],
        "leg_metrics":        leg_pack["metrics"],
    }

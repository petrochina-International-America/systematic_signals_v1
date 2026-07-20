import numpy as np
import pandas as pd

from energy.accounting.contract_specs import CONTRACT_SPECS
from energy.accounting.mtm import build_roll_path


# ============================================================
# ROLL CONFIG / PREP
# ============================================================
def _get_roll_config(commodity_name: str, roll_config: str = "prompt_EOM_roll") -> dict:
    spec = CONTRACT_SPECS.get(commodity_name, {})
    cfg = spec.get(roll_config)
    if cfg is None:
        raise ValueError(f"{commodity_name}: no '{roll_config}' config in CONTRACT_SPECS")
    return cfg


def _build_leg_roll_path(
    commodity_name: str,
    prices: pd.DataFrame,
    expiry_calendar: pd.Series,
    roll_config: str = "prompt_EOM_roll",
) -> pd.DataFrame:
    cfg = _get_roll_config(commodity_name, roll_config)
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


def _prepare_leg(
    commodity_name: str,
    prices: pd.DataFrame,
    expiry_calendar: pd.Series,
    roll_config: str = "prompt_EOM_roll",
) -> pd.DataFrame:
    rolled_df = _build_leg_roll_path(commodity_name, prices, expiry_calendar, roll_config)
    out = rolled_df.copy()
    out["held_price"] = _extract_held_price(prices, out["held_contract"])
    return out


# ============================================================
# SPREAD
# ============================================================
def _build_spread(
    leg1_df: pd.DataFrame,
    leg2_df: pd.DataFrame,
    norm1: float = 1.0,
    norm2: float = 1.0,
) -> pd.Series:
    """
    Spread on the intersection of both legs' trading calendars, in $/BBL equivalent.

    norm1/norm2 exist for RAW (unnormalized) price frames only. All current
    callers load prices pre-normalized to $/BBL (load_prices normalize=True /
    loader.get_prices_normalized) and MUST pass 1.0 — applying the
    CONTRACT_SPECS factor again double-normalizes cross-unit pairs
    (e.g. ULSD re-scaled 0.42x against WTI; the 2026-07-08 audit's
    blend-engine defect). For same-unit pairs an extra common factor only
    rescales the spread and the signal is unchanged.
    """
    idx = leg1_df.index.intersection(leg2_df.index).sort_values()
    out = pd.DataFrame(index=idx)
    out["px1"] = leg1_df["held_price"].reindex(idx) * norm1
    out["px2"] = leg2_df["held_price"].reindex(idx) * norm2
    return (out["px1"] - out["px2"]).rename("spread")


# ============================================================
# SIGNAL — % DEVIATION FROM ROLLING MEAN
# ============================================================
def _build_signal(
    spread: pd.Series,
    lookback: int = 60,
    deviation_band_pct: float = 0.15,
    exit_threshold: float | None = None,
) -> pd.DataFrame:
    """
    Entry when (spread - rolling_mean) / |rolling_mean| exceeds deviation_band_pct.

    Exit behaviour controlled by exit_threshold:
      exit_threshold=None (default): exit when spread crosses rolling mean.
      exit_threshold=0.0: exit when deviation_pct crosses zero — flat whenever
          |dev_pct| < entry band, positioned whenever |dev_pct| > entry band.
      exit_threshold=k (0 < k < deviation_band_pct): exit when |dev_pct| drops
          below k — holds longer than zero-cross but exits before opposite band.

    deviation_pct = (spread - mean) / |mean|

    Signal convention:
    +1 = long spread  (long leg1 / short leg2)
    -1 = short spread (short leg1 / long leg2)
    """
    s = pd.Series(spread).astype(float)

    spread_mean = s.rolling(window=lookback, min_periods=lookback).mean()
    deviation   = s - spread_mean
    denom       = spread_mean.abs().replace(0, np.nan)
    deviation_pct = deviation / denom

    upper_band = spread_mean + deviation_band_pct * spread_mean.abs()
    lower_band = spread_mean - deviation_band_pct * spread_mean.abs()

    signal_raw = pd.Series(index=s.index, dtype=float, name="signal_raw")
    state = 0.0

    for i, dt in enumerate(s.index):
        if i == 0:
            signal_raw.loc[dt] = 0.0
            continue

        prev_dt      = s.index[i - 1]
        prev_spread  = s.loc[prev_dt]
        prev_mean    = spread_mean.loc[prev_dt]
        prev_dev_pct = deviation_pct.loc[prev_dt]

        if pd.isna(prev_spread) or pd.isna(prev_mean) or pd.isna(prev_dev_pct):
            signal_raw.loc[dt] = 0.0
            state = 0.0
            continue

        if state == 0.0:
            if prev_dev_pct >= deviation_band_pct:
                state = -1.0
            elif prev_dev_pct <= -deviation_band_pct:
                state = 1.0
        elif exit_threshold is None:
            # Original: exit when spread crosses rolling mean
            if state == 1.0 and prev_spread >= prev_mean:
                state = 0.0
            elif state == -1.0 and prev_spread <= prev_mean:
                state = 0.0
        else:
            # Threshold-based exit: go flat when |dev_pct| drops inside exit_threshold
            if state == 1.0 and prev_dev_pct >= -exit_threshold:
                state = 0.0
            elif state == -1.0 and prev_dev_pct <= exit_threshold:
                state = 0.0

        signal_raw.loc[dt] = state

    out = pd.DataFrame(index=s.index)
    out["spread"]        = s
    out["spread_mean"]   = spread_mean
    out["deviation"]     = deviation
    out["deviation_pct"] = deviation_pct
    out["upper_band"]    = upper_band
    out["lower_band"]    = lower_band
    out["signal_raw"]    = signal_raw.fillna(0.0)

    valid_mask = spread_mean.notna()
    out.attrs["start_date"] = (
        out.index[np.argmax(valid_mask.to_numpy())] if valid_mask.any() else out.index[0]
    )
    return out


# ============================================================
# PAIR PNL
# ============================================================
def _build_pair_pnl(
    leg1_df: pd.DataFrame,
    leg2_df: pd.DataFrame,
) -> pd.DataFrame:
    idx = leg1_df.index.intersection(leg2_df.index).sort_values()
    out = pd.DataFrame(index=idx)
    out["leg1_daily_pnl"] = leg1_df["daily_pnl"].reindex(idx).fillna(0.0)
    out["leg2_daily_pnl"] = leg2_df["daily_pnl"].reindex(idx).fillna(0.0)
    roll1 = leg1_df["roll_day_flag"].reindex(idx).fillna(0).astype(int)
    roll2 = leg2_df["roll_day_flag"].reindex(idx).fillna(0).astype(int)
    out["roll_day_flag"]  = np.maximum(roll1, roll2)
    hc1 = leg1_df["held_contract"].reindex(idx).fillna("NA").astype(str)
    hc2 = leg2_df["held_contract"].reindex(idx).fillna("NA").astype(str)
    out["held_contract"]  = hc1 + " | " + hc2
    return out


# ============================================================
# MTM CAPITAL LOOP
# ============================================================
def _capital_loop(
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
) -> pd.DataFrame:
    """
    Vectorized capital loop: iterates over rebalance segments rather than
    every day. Between rebalances contracts are constant, so PnL and capital
    accumulate via numpy cumsum — O(R·N) → O(R + N) where R << N.
    """
    idx = signal_raw.index
    n   = len(idx)

    if vol_min_obs is None:
        vol_min_obs = vol_window

    target_daily_vol = vol_target_ann / np.sqrt(252.0)

    sig  = signal_raw.to_numpy(dtype=float)
    pnl1 = leg1_pnl_price.reindex(idx).fillna(0.0).to_numpy(dtype=float)
    pnl2 = leg2_pnl_price.reindex(idx).fillna(0.0).to_numpy(dtype=float)
    px1  = leg1_held_price.reindex(idx).to_numpy(dtype=float)
    px2  = leg2_held_price.reindex(idx).to_numpy(dtype=float)
    roll = roll_day_flag.reindex(idx).fillna(0).astype(int).to_numpy()

    w1         = 0.5 * sig
    w2         = -0.5 * sig
    prev_w1    = np.concatenate([[0.0], w1[:-1]])
    prev_w2    = np.concatenate([[0.0], w2[:-1]])
    sig_change = ((w1 != prev_w1) | (w2 != prev_w2)).astype(int)
    live_roll  = ((roll == 1) & ((w1 != 0.0) | (w2 != 0.0))).astype(int)
    reb        = np.maximum(sig_change, live_roll)
    reb[0]     = 1

    # Pre-fill forward-valid prices once (O(N), avoids repeated backward scan)
    def _ffill_pos(arr: np.ndarray) -> np.ndarray:
        out  = arr.copy()
        last = np.nan
        for i in range(len(out)):
            v = out[i]
            if not np.isnan(v) and v > 0:
                last = v
            out[i] = last
        return out

    px1_safe = _ffill_pos(px1)
    px2_safe = _ffill_pos(px2)

    reb_times = np.where(reb)[0]   # rebalance points — typically R << N

    contracts1  = np.zeros(n, dtype=float)
    contracts2  = np.zeros(n, dtype=float)
    leg1_dollar = np.zeros(n, dtype=float)
    leg2_dollar = np.zeros(n, dtype=float)
    txn_cost    = np.zeros(n, dtype=float)
    capital     = np.full(n, np.nan, dtype=float)
    daily_ret   = np.full(n, np.nan, dtype=float)
    self_vol    = np.full(n, np.nan, dtype=float)
    vol_scalar  = np.ones(n, dtype=float)

    # Vol-estimator input: the pair's own daily return, position-independent.
    # Sizing on the strategy's own equity returns (daily_ret) is forbidden
    # here: flat streaks write exact zeros into the window, collapse the vol
    # estimate, and blow up position size at re-entry.
    px1_prev = np.concatenate([[np.nan], px1_safe[:-1]])
    px2_prev = np.concatenate([[np.nan], px2_safe[:-1]])
    with np.errstate(divide="ignore", invalid="ignore"):
        pair_ret = 0.5 * pnl1 / px1_prev - 0.5 * pnl2 / px2_prev

    prev_n1  = 0.0
    prev_n2  = 0.0
    prev_cap = initial_capital

    for seg_i, t_s in enumerate(reb_times):
        t_e = int(reb_times[seg_i + 1]) if seg_i + 1 < len(reb_times) else n

        # ── PnL at the rebalance point using previous-segment contracts ──
        if t_s > 0:
            dp1 = prev_n1 * pnl1[t_s] * mult1
            dp2 = prev_n2 * pnl2[t_s] * mult2
            leg1_dollar[t_s] = dp1
            leg2_dollar[t_s] = dp2
            gross    = dp1 + dp2
            cap_pre  = prev_cap + gross
            daily_ret[t_s] = gross / prev_cap if prev_cap > 0 else np.nan
        else:
            cap_pre = prev_cap

        # ── Vol scalar (only used when vol_window > 0) ───────────────────
        if vol_window > 0:
            hist  = pair_ret[:t_s]
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

        # ── New contracts ────────────────────────────────────────────────
        px1_t = px1_safe[t_s]
        px2_t = px2_safe[t_s]
        s_t   = sig[t_s]
        if s_t != 0 and not np.isnan(px1_t) and not np.isnan(px2_t) and cap_pre > 0:
            new_n1 =  0.5 * sc * s_t * cap_pre / (px1_t * mult1)
            new_n2 = -0.5 * sc * s_t * cap_pre / (px2_t * mult2)
        else:
            new_n1 = new_n2 = 0.0

        cost_t    = abs(new_n1 - prev_n1) * tc1 + abs(new_n2 - prev_n2) * tc2
        cap_after = cap_pre - cost_t

        contracts1[t_s] = new_n1
        contracts2[t_s] = new_n2
        txn_cost[t_s]   = cost_t
        capital[t_s]    = cap_after

        # ── Vectorize the between-rebalance segment [t_s+1, t_e) ────────
        seg = np.arange(t_s + 1, t_e)
        if len(seg):
            contracts1[seg]  = new_n1
            contracts2[seg]  = new_n2
            vol_scalar[seg]  = sc

            dp1_seg = new_n1 * pnl1[seg] * mult1
            dp2_seg = new_n2 * pnl2[seg] * mult2
            leg1_dollar[seg] = dp1_seg
            leg2_dollar[seg] = dp2_seg

            gross_seg  = dp1_seg + dp2_seg
            cum_gross  = np.cumsum(gross_seg)
            capital[seg] = cap_after + cum_gross

            prev_caps = np.empty(len(seg))
            prev_caps[0]  = cap_after
            prev_caps[1:] = cap_after + cum_gross[:-1]
            daily_ret[seg] = np.where(prev_caps > 0, gross_seg / prev_caps, np.nan)

        prev_n1  = new_n1
        prev_n2  = new_n2
        prev_cap = capital[t_e - 1] if t_e > 0 else cap_after

    cap_s     = pd.Series(capital, index=idx)
    gross_pnl = pd.Series(leg1_dollar + leg2_dollar, index=idx)
    tc_s      = pd.Series(txn_cost, index=idx)

    out = pd.DataFrame(index=idx)
    out["signal_raw"]      = sig
    out["leg1_contracts"]  = contracts1
    out["leg2_contracts"]  = contracts2
    out["leg1_dollar_pnl"] = leg1_dollar
    out["leg2_dollar_pnl"] = leg2_dollar
    out["gross_pnl"]       = gross_pnl
    out["t_cost"]          = tc_s
    out["net_pnl"]         = gross_pnl - tc_s
    out["capital"]         = cap_s
    out["daily_ret"]       = pd.Series(daily_ret, index=idx)
    out["equity_index"]    = cap_s / initial_capital
    out["rebalance_flag"]  = reb
    out["roll_day_flag"]   = roll
    out["self_vol"]        = pd.Series(self_vol, index=idx)
    out["vol_scalar"]      = pd.Series(vol_scalar, index=idx)
    return out


# ============================================================
# LEG DECOMPOSITION
# ============================================================
def _compute_perf_metrics(pnl: pd.Series, annualization: int = 252) -> dict:
    pnl       = pd.Series(pnl).fillna(0.0).astype(float)
    total_pnl = pnl.sum()
    avg_daily = pnl.mean()
    daily_vol = pnl.std(ddof=0)
    ann_pnl   = avg_daily * annualization
    ann_vol   = daily_vol * np.sqrt(annualization)
    sharpe    = np.nan if ann_vol == 0 else ann_pnl / ann_vol
    equity    = pnl.cumsum()
    hwm       = equity.cummax()
    max_dd    = (equity - hwm).min()
    rod       = np.nan if max_dd >= 0 else total_pnl / abs(max_dd)
    hit_rate  = (pnl > 0).mean()
    return {
        "Total PnL": total_pnl, "Annual PnL": ann_pnl,
        "Annualized Vol": ann_vol, "Sharpe": sharpe,
        "Max Drawdown": max_dd, "Return on Drawdown": rod, "Hit Rate": hit_rate,
    }


def _leg_decomposition(strategy_df: pd.DataFrame, leg1_name: str, leg2_name: str) -> dict:
    contrib = pd.DataFrame(index=strategy_df.index)
    contrib[leg1_name]    = strategy_df["leg1_dollar_pnl"].fillna(0.0)
    contrib[leg2_name]    = strategy_df["leg2_dollar_pnl"].fillna(0.0)
    contrib["gross_pnl"]  = contrib[leg1_name] + contrib[leg2_name]
    contrib["net_pnl"]    = strategy_df["net_pnl"].fillna(0.0)
    cumulative = contrib.cumsum()
    metrics = pd.DataFrame({
        leg1_name:   _compute_perf_metrics(contrib[leg1_name]),
        leg2_name:   _compute_perf_metrics(contrib[leg2_name]),
        "gross_pnl": _compute_perf_metrics(contrib["gross_pnl"]),
        "net_pnl":   _compute_perf_metrics(contrib["net_pnl"]),
    }).T
    return {"daily_pnl": contrib, "cumulative_pnl": cumulative, "metrics": metrics}


# ============================================================
# PUBLIC API
# ============================================================
def rv_pct_deviation(
    leg1_name: str,
    leg2_name: str,
    prices1: pd.DataFrame,
    prices2: pd.DataFrame,
    expiry1: pd.Series,
    expiry2: pd.Series,
    initial_capital: float = 1_000_000.0,
    lookback: int = 60,
    deviation_band_pct: float = 0.15,
    exit_threshold: float | None = None,
    vol_window: int = 0,
    vol_target_ann: float = 0.15,
    trade_start: str | None = None,
    roll_config: str = "prompt_EOM_roll",
    signal_roll_config: str | None = None,
) -> dict:
    """
    MTM stat-arb using % deviation from rolling mean as the entry signal.

    Entry:  |(spread - mean) / |mean|| > deviation_band_pct
    Exit:   controlled by exit_threshold (see _build_signal docstring).
            exit_threshold=None  → exit when spread crosses rolling mean (default).
            exit_threshold=0.0   → exit when |dev_pct| crosses zero.
            exit_threshold=k     → exit when |dev_pct| drops below k.
    Sizing: capital-proportional contracts (±0.5 each leg)
    Vol:    optional vol targeting in the sizing layer (set vol_window=0 to disable)

    trade_start : ISO date string e.g. "2015-01-01".
        Signal is computed on the full price history (so the rolling mean is
        warmed up by trade_start), but the capital loop only begins at
        trade_start.  The equity index therefore starts at 1.0 on trade_start.
        Pass None to run from the first available date.
    roll_config : key in CONTRACT_SPECS to use for the execution roll path
        (PnL computation and contract sizing).
        "prompt_EOM_roll" (default) uses front-month contracts.
        "1yr_deferred_roll" uses F12/F13 contracts.
    signal_roll_config : if provided, compute the entry/exit signal from this
        roll path instead of roll_config.  Useful for "monitor prompt spread,
        execute in deferred" strategies.  When None (default) the same roll
        path is used for both signal and execution.
    """
    # Execution legs — used for PnL, sizing, and held prices
    leg1_df = _prepare_leg(leg1_name, prices1, expiry1, roll_config)
    leg2_df = _prepare_leg(leg2_name, prices2, expiry2, roll_config)

    # Signal legs — separate roll config when requested
    if signal_roll_config is not None and signal_roll_config != roll_config:
        sig_leg1_df = _prepare_leg(leg1_name, prices1, expiry1, signal_roll_config)
        sig_leg2_df = _prepare_leg(leg2_name, prices2, expiry2, signal_roll_config)
    else:
        sig_leg1_df = leg1_df
        sig_leg2_df = leg2_df

    spec1 = CONTRACT_SPECS.get(leg1_name, {})
    spec2 = CONTRACT_SPECS.get(leg2_name, {})
    mult1 = float(spec1.get("contract_multiplier", 1))
    mult2 = float(spec2.get("contract_multiplier", 1))
    tc1   = float(spec1.get("t_cost_abs", 0.0))
    tc2   = float(spec2.get("t_cost_abs", 0.0))

    # Prices are already normalized to $/BBL at load time (load_prices normalize=True).
    # Passing norm=1.0 avoids double-normalization for NGL commodities.
    signal_spread = _build_spread(sig_leg1_df, sig_leg2_df, 1.0, 1.0)
    spread        = _build_spread(leg1_df, leg2_df, 1.0, 1.0)
    signal_df = _build_signal(signal_spread, lookback, deviation_band_pct, exit_threshold)
    pair_pnl  = _build_pair_pnl(leg1_df, leg2_df)

    idx = signal_df.index.intersection(pair_pnl.index).sort_values()
    if trade_start is not None:
        idx = idx[idx >= pd.Timestamp(trade_start)]
    signal_raw = signal_df["signal_raw"].reindex(idx).fillna(0.0)

    strategy_df = _capital_loop(
        signal_raw      = signal_raw,
        leg1_pnl_price  = pair_pnl["leg1_daily_pnl"].reindex(idx).fillna(0.0),
        leg2_pnl_price  = pair_pnl["leg2_daily_pnl"].reindex(idx).fillna(0.0),
        leg1_held_price = leg1_df["held_price"].reindex(idx),
        leg2_held_price = leg2_df["held_price"].reindex(idx),
        roll_day_flag   = pair_pnl["roll_day_flag"].reindex(idx).fillna(0),
        mult1=mult1, mult2=mult2, tc1=tc1, tc2=tc2,
        initial_capital=initial_capital,
        vol_window=vol_window, vol_target_ann=vol_target_ann,
    )

    for col in ["spread", "spread_mean", "deviation", "deviation_pct", "upper_band", "lower_band"]:
        if col in signal_df.columns:
            strategy_df[col] = signal_df[col].reindex(idx)
    strategy_df["held_contract"] = pair_pnl["held_contract"].reindex(idx)

    leg_pack = _leg_decomposition(strategy_df, leg1_name, leg2_name)

    return {
        "strategy_df":        strategy_df,
        "signal_df":          signal_df,
        "spread":             spread,
        "signal_spread":      signal_spread,
        "leg1_df":            leg1_df,
        "leg2_df":            leg2_df,
        "pair_pnl_df":        pair_pnl,
        "leg_daily_pnl":      leg_pack["daily_pnl"],
        "leg_cumulative_pnl": leg_pack["cumulative_pnl"],
        "leg_metrics":        leg_pack["metrics"],
    }


def rv_pct_deviation_blend(
    leg1_name: str,
    leg2_name: str,
    prices1: pd.DataFrame,
    prices2: pd.DataFrame,
    expiry1: pd.Series,
    expiry2: pd.Series,
    initial_capital: float = 1_000_000.0,
    fast_lookback: int = 20,
    fast_band: float = 0.05,
    slow_lookback: int = 60,
    slow_band: float = 0.15,
    vol_window: int = 0,
    vol_target_ann: float = 0.15,
    trade_start: str | None = None,
    roll_config: str = "prompt_EOM_roll",
) -> dict:
    """
    Consensus blend of a fast and slow pct-deviation signal.

    Entry:  fast_signal == slow_signal != 0  (both timeframes agree on direction)
    Exit:   either signal returns to 0       (conservative — exit on first disagreement)
    Sizing: capital-proportional contracts via the same MTM capital loop

    The consensus filter removes trades where the two lookbacks disagree,
    keeping only higher-conviction dislocations visible on both timeframes.
    trade_start : see rv_pct_deviation for semantics.
    roll_config : see rv_pct_deviation for semantics.
    """
    leg1_df  = _prepare_leg(leg1_name, prices1, expiry1, roll_config)
    leg2_df  = _prepare_leg(leg2_name, prices2, expiry2, roll_config)

    # Prices are already normalized to $/BBL at load time (load_prices normalize=True).
    # Passing norm=1.0 avoids double-normalization for cross-unit pairs.
    spread   = _build_spread(leg1_df, leg2_df, 1.0, 1.0)
    pair_pnl = _build_pair_pnl(leg1_df, leg2_df)

    fast_df = _build_signal(spread, fast_lookback, fast_band)
    slow_df = _build_signal(spread, slow_lookback, slow_band)

    idx = fast_df.index.intersection(slow_df.index).sort_values()
    if trade_start is not None:
        idx = idx[idx >= pd.Timestamp(trade_start)]
    fast_sig = fast_df["signal_raw"].reindex(idx).fillna(0.0)
    slow_sig = slow_df["signal_raw"].reindex(idx).fillna(0.0)

    # Consensus: non-zero only when both agree in direction
    consensus = pd.Series(
        np.where(
            (fast_sig == slow_sig) & (fast_sig != 0.0),
            fast_sig,
            0.0,
        ),
        index=idx,
        name="signal_raw",
    )

    spec1 = CONTRACT_SPECS.get(leg1_name, {})
    spec2 = CONTRACT_SPECS.get(leg2_name, {})
    mult1 = float(spec1.get("contract_multiplier", 1))
    mult2 = float(spec2.get("contract_multiplier", 1))
    tc1   = float(spec1.get("t_cost_abs", 0.0))
    tc2   = float(spec2.get("t_cost_abs", 0.0))

    idx2 = consensus.index.intersection(pair_pnl.index).sort_values()

    strategy_df = _capital_loop(
        signal_raw      = consensus.reindex(idx2).fillna(0.0),
        leg1_pnl_price  = pair_pnl["leg1_daily_pnl"].reindex(idx2).fillna(0.0),
        leg2_pnl_price  = pair_pnl["leg2_daily_pnl"].reindex(idx2).fillna(0.0),
        leg1_held_price = leg1_df["held_price"].reindex(idx2),
        leg2_held_price = leg2_df["held_price"].reindex(idx2),
        roll_day_flag   = pair_pnl["roll_day_flag"].reindex(idx2).fillna(0),
        mult1=mult1, mult2=mult2, tc1=tc1, tc2=tc2,
        initial_capital=initial_capital,
        vol_window=vol_window, vol_target_ann=vol_target_ann,
    )

    strategy_df["fast_signal"] = fast_sig.reindex(idx2)
    strategy_df["slow_signal"] = slow_sig.reindex(idx2)
    for col in ["spread", "spread_mean", "deviation", "deviation_pct"]:
        if col in slow_df.columns:
            strategy_df[col] = slow_df[col].reindex(idx2)
    strategy_df["held_contract"] = pair_pnl["held_contract"].reindex(idx2)

    leg_pack = _leg_decomposition(strategy_df, leg1_name, leg2_name)

    return {
        "strategy_df":        strategy_df,
        "fast_signal_df":     fast_df,
        "slow_signal_df":     slow_df,
        "spread":             spread,
        "leg1_df":            leg1_df,
        "leg2_df":            leg2_df,
        "pair_pnl_df":        pair_pnl,
        "leg_daily_pnl":      leg_pack["daily_pnl"],
        "leg_cumulative_pnl": leg_pack["cumulative_pnl"],
        "leg_metrics":        leg_pack["metrics"],
    }

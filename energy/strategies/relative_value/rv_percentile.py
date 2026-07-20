import numpy as np
import pandas as pd

from energy.accounting.contract_specs import CONTRACT_SPECS
from energy.accounting.mtm import build_roll_path


# ============================================================
# ROLL CONFIG / PREP  (identical to rv_pct_deviation)
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
) -> pd.Series:
    idx = leg1_df.index.intersection(leg2_df.index).sort_values()
    out = pd.DataFrame(index=idx)
    out["px1"] = leg1_df["held_price"].reindex(idx)
    out["px2"] = leg2_df["held_price"].reindex(idx)
    return (out["px1"] - out["px2"]).rename("spread")


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
    out["roll_day_flag"] = np.maximum(roll1, roll2)
    hc1 = leg1_df["held_contract"].reindex(idx).fillna("NA").astype(str)
    hc2 = leg2_df["held_contract"].reindex(idx).fillna("NA").astype(str)
    out["held_contract"] = hc1 + " | " + hc2
    return out


# ============================================================
# SIGNAL — PERCENTILE RANK
# ============================================================
def _build_percentile_signal(
    spread: pd.Series,
    lookback: int = 60,
    entry_pct: float = 0.10,
) -> pd.DataFrame:
    """
    Entry when the spread's rolling percentile rank hits an extreme.

    entry_pct = 0.10 means:
      - Enter SHORT spread when rank >= (1 - entry_pct) = 90th percentile
        (spread historically expensive → expect reversion down)
      - Enter LONG  spread when rank <= entry_pct = 10th percentile
        (spread historically cheap → expect reversion up)
    Exit when rank crosses back through the 50th percentile (rolling median).

    Signal convention:
      +1 = long spread  (long leg1 / short leg2)
      -1 = short spread (short leg1 / long leg2)

    No normalization by level or vol — purely distributional.
    """
    s = pd.Series(spread).astype(float)

    # Vectorized rolling rank: pandas rolling().rank() gives 1-based rank within window.
    # Normalise to [0, 1] by dividing by lookback.
    rank = (
        s.rolling(window=lookback, min_periods=lookback)
         .rank(pct=True)          # pct=True → already in (0, 1]
    ).rename("percentile_rank")

    median = s.rolling(window=lookback, min_periods=lookback).median().rename("spread_median")

    upper_entry = 1.0 - entry_pct
    lower_entry = entry_pct

    # Work in numpy for speed — avoid per-row iloc overhead
    r_arr  = rank.to_numpy(dtype=float)
    m_arr  = median.to_numpy(dtype=float)
    s_arr  = s.to_numpy(dtype=float)
    n      = len(s_arr)
    sig    = np.zeros(n, dtype=float)
    state  = 0.0

    for i in range(1, n):
        pr = r_arr[i - 1]
        pm = m_arr[i - 1]
        ps = s_arr[i - 1]

        if np.isnan(pr) or np.isnan(pm):
            state = 0.0
        elif state == 0.0:
            if pr >= upper_entry:
                state = -1.0
            elif pr <= lower_entry:
                state = 1.0
        elif state == 1.0:
            if ps >= pm:
                state = 0.0
        elif state == -1.0:
            if ps <= pm:
                state = 0.0

        sig[i] = state

    signal_raw = pd.Series(sig, index=s.index, name="signal_raw")

    out = pd.DataFrame(index=s.index)
    out["spread"]          = s
    out["spread_median"]   = median
    out["percentile_rank"] = rank
    out["upper_entry"]     = upper_entry
    out["lower_entry"]     = lower_entry
    out["signal_raw"]      = signal_raw.fillna(0.0)

    valid_mask = rank.notna()
    out.attrs["start_date"] = (
        out.index[np.argmax(valid_mask.to_numpy())] if valid_mask.any() else out.index[0]
    )
    return out


# ============================================================
# MTM CAPITAL LOOP  (identical to rv_pct_deviation)
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

    reb_times = np.where(reb)[0]

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
# PUBLIC API
# ============================================================
def rv_percentile(
    leg1_name: str,
    leg2_name: str,
    prices1: pd.DataFrame,
    prices2: pd.DataFrame,
    expiry1: pd.Series,
    expiry2: pd.Series,
    initial_capital: float = 1_000_000.0,
    lookback: int = 60,
    entry_pct: float = 0.10,
    vol_window: int = 0,
    vol_target_ann: float = 0.15,
    trade_start: str | None = None,
    roll_config: str = "prompt_EOM_roll",
    signal_roll_config: str | None = None,
) -> dict:
    """
    MTM stat-arb using rolling percentile rank as the entry signal.

    Entry:  rank <= entry_pct  → long spread  (spread historically cheap)
            rank >= 1-entry_pct → short spread (spread historically expensive)
    Exit:   spread crosses back through rolling median
    Sizing: capital-proportional contracts (±0.5 each leg)
    Vol:    optional vol targeting (set vol_window=0 to disable)

    entry_pct=0.10 means enter on bottom/top 10% of rolling distribution.
    No normalization by level or vol — purely distributional.
    """
    leg1_df = _prepare_leg(leg1_name, prices1, expiry1, roll_config)
    leg2_df = _prepare_leg(leg2_name, prices2, expiry2, roll_config)

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

    signal_spread = _build_spread(sig_leg1_df, sig_leg2_df)
    pair_pnl      = _build_pair_pnl(leg1_df, leg2_df)

    signal_df  = _build_percentile_signal(signal_spread, lookback, entry_pct)

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

    for col in ["spread", "spread_median", "percentile_rank", "upper_entry", "lower_entry"]:
        if col in signal_df.columns:
            strategy_df[col] = signal_df[col].reindex(idx)
    strategy_df["held_contract"] = pair_pnl["held_contract"].reindex(idx)

    return {
        "strategy_df":   strategy_df,
        "signal_df":     signal_df,
        "signal_spread": signal_spread,
        "leg1_df":       leg1_df,
        "leg2_df":       leg2_df,
        "pair_pnl_df":   pair_pnl,
    }

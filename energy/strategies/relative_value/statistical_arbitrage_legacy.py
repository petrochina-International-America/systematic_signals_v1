import warnings

import pandas as pd
import numpy as np

warnings.warn(
    "statistical_arbitrage_legacy uses the same-close booking convention "
    "retired on 2026-07-09. Historical reference only — use "
    "statistical_arbitrage (capital-loop timing).",
    DeprecationWarning, stacklevel=2,
)


def _build_stat_arb_signal(
    prices: pd.DataFrame,
    zscore: pd.Series | None = None,
    spread_col: str | None = None,
    entry_z: float = 1.0,
    exit_z: float = 0.0,
) -> tuple[pd.Series, pd.Timestamp]:
    """
    Build raw stat-arb signal and return:
        signal_raw : {-1,0,+1} signal at date t
        start_date : first date strategy is allowed to trade

    Convention:
    - positive z-score => rich => short
    - negative z-score => cheap => long
    """
    df = prices.copy()

    if zscore is not None:
        z = zscore.reindex(df.index).astype(float)
    elif spread_col is not None:
        spread = df[spread_col].astype(float)
        mu = spread.rolling(20, min_periods=20).mean()
        sd = spread.rolling(20, min_periods=20).std()
        z = (spread - mu) / sd.replace(0, np.nan)
    else:
        raise ValueError("Provide either zscore or spread_col")

    signal_raw = pd.Series(0.0, index=df.index, name="signal_raw", dtype=float)

    signal_raw[z >= entry_z] = -1.0
    signal_raw[z <= -entry_z] = 1.0
    signal_raw[z.abs() <= exit_z] = 0.0

    signal_raw = signal_raw.ffill().fillna(0.0)

    valid_mask = z.notna()
    if valid_mask.any():
        start_date = df.index[valid_mask.argmax()]
    else:
        start_date = df.index[0]

    return signal_raw, start_date


def legacy_capstone_statistical_arbitrage(
    prices: pd.DataFrame,
    rolled_df: pd.DataFrame,
    zscore: pd.Series | None = None,
    spread_col: str | None = None,
    front_col: str = "F1",
    t_cost: float = 0.00,
    pct_t_cost: float | None = None,
    entry_z: float = 1.0,
    exit_z: float = 0.0,
) -> pd.DataFrame:
    """
    Legacy capstone statistical arbitrage.
    """
    signal_raw, start_date = _build_stat_arb_signal(
        prices=prices,
        zscore=zscore,
        spread_col=spread_col,
        entry_z=entry_z,
        exit_z=exit_z,
    )

    pnl_df = rolled_df[["daily_pnl", "t_cost", "roll_day_flag"]].copy()

    pnl_df["signal"] = signal_raw.reindex(pnl_df.index).fillna(0.0)
    pnl_df["signal_lag"] = pnl_df["signal"].shift(1).fillna(0.0)

    pnl_df.loc[pnl_df.index < start_date, ["signal", "signal_lag"]] = 0.0
    pnl_df.loc[pnl_df.index < start_date, "roll_day_flag"] = 0

    pnl_df["stat_arb_raw"] = pnl_df["signal_lag"] * pnl_df["daily_pnl"]

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

    pnl_df["net_pnl"] = pnl_df["stat_arb_raw"] - pnl_df["t_cost"]
    pnl_df["equity_line"] = pnl_df["net_pnl"].cumsum()

    out = pnl_df.rename(columns={"stat_arb_raw": "daily_pnl", "roll_day_flag": "roll_flag"})
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


def statistical_arbitrage(
    prices: pd.DataFrame,
    rolled_df: pd.DataFrame,
    zscore: pd.Series | None = None,
    spread_col: str | None = None,
    entry_z: float = 1.0,
    exit_z: float = 0.0,
) -> pd.DataFrame:
    """
    MTM-ready statistical arbitrage path.
    """
    signal_raw, start_date = _build_stat_arb_signal(
        prices=prices,
        zscore=zscore,
        spread_col=spread_col,
        entry_z=entry_z,
        exit_z=exit_z,
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


def _compute_perf_metrics(
    pnl: pd.Series,
    annualization: int = 252,
) -> dict:
    """
    Standard performance metrics for a daily PnL stream.
    """
    pnl = pnl.fillna(0.0).astype(float)

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

    return_on_drawdown = np.nan
    if max_drawdown < 0:
        return_on_drawdown = total_pnl / abs(max_drawdown)

    hit_rate = (pnl > 0).mean()

    return {
        "Total PnL": total_pnl,
        "Annual PnL": ann_pnl,
        "Annualized Vol": ann_vol,
        "Sharpe": sharpe,
        "Max Drawdown": max_drawdown,
        "Return on Drawdown": return_on_drawdown,
        "Hit Rate": hit_rate,
    }


def build_stat_arb_leg_pnl(
    strategy_df: pd.DataFrame,
    leg1_daily_pnl: pd.Series,
    leg2_daily_pnl: pd.Series,
    leg1_name: str = "leg_1",
    leg2_name: str = "leg_2",
    leg1_weight: float = 1.0,
    leg2_weight: float = -1.0,
) -> pd.DataFrame:
    """
    Build signed leg-level PnL streams from the stat-arb held position.

    Parameters
    ----------
    strategy_df
        Output of statistical_arbitrage(...), must contain 'position'.
    leg1_daily_pnl, leg2_daily_pnl
        Raw 1-unit daily PnL streams for each leg, aligned by date.
    leg1_weight, leg2_weight
        Optional hedge weights. Defaults to symmetric long/short setup.
    """
    if "position" not in strategy_df.columns:
        raise ValueError("strategy_df must contain a 'position' column from statistical_arbitrage(...)")

    idx = strategy_df.index.intersection(leg1_daily_pnl.index).intersection(leg2_daily_pnl.index).sort_values()

    out = pd.DataFrame(index=idx)

    out["position"] = strategy_df["position"].reindex(idx).fillna(0.0).astype(float)

    out[f"{leg1_name}_daily_pnl"] = leg1_daily_pnl.reindex(idx).fillna(0.0).astype(float)
    out[f"{leg2_name}_daily_pnl"] = leg2_daily_pnl.reindex(idx).fillna(0.0).astype(float)

    out[f"{leg1_name}_pnl"] = leg1_weight * out["position"] * out[f"{leg1_name}_daily_pnl"]
    out[f"{leg2_name}_pnl"] = leg2_weight * out["position"] * out[f"{leg2_name}_daily_pnl"]

    out["strategy_pnl_from_legs"] = out[f"{leg1_name}_pnl"] + out[f"{leg2_name}_pnl"]

    return out


def stat_arb_leg_analytics(
    strategy_df: pd.DataFrame,
    leg1_daily_pnl: pd.Series,
    leg2_daily_pnl: pd.Series,
    leg1_name: str = "leg_1",
    leg2_name: str = "leg_2",
    leg1_weight: float = 1.0,
    leg2_weight: float = -1.0,
    annualization: int = 252,
) -> dict[str, pd.DataFrame]:
    """
    Leg-level PnL attribution and diagnostics for statistical arbitrage.

    Returns a dict with:
    - daily_pnl
    - cumulative_pnl
    - metrics
    - contribution
    """
    leg_df = build_stat_arb_leg_pnl(
        strategy_df=strategy_df,
        leg1_daily_pnl=leg1_daily_pnl,
        leg2_daily_pnl=leg2_daily_pnl,
        leg1_name=leg1_name,
        leg2_name=leg2_name,
        leg1_weight=leg1_weight,
        leg2_weight=leg2_weight,
    )

    series_map = {
        leg1_name: leg_df[f"{leg1_name}_pnl"],
        leg2_name: leg_df[f"{leg2_name}_pnl"],
        "strategy": leg_df["strategy_pnl_from_legs"],
    }

    metrics = pd.DataFrame(
        {name: _compute_perf_metrics(pnl, annualization=annualization)
         for name, pnl in series_map.items()}
    ).T

    cumulative = pd.DataFrame(
        {name: pnl.cumsum() for name, pnl in series_map.items()},
        index=leg_df.index,
    )

    contribution = pd.DataFrame(index=["Total PnL Contribution"])
    total_strategy_pnl = series_map["strategy"].sum()

    if total_strategy_pnl != 0:
        contribution[leg1_name] = [series_map[leg1_name].sum() / total_strategy_pnl]
        contribution[leg2_name] = [series_map[leg2_name].sum() / total_strategy_pnl]
    else:
        contribution[leg1_name] = [np.nan]
        contribution[leg2_name] = [np.nan]

    contribution["strategy"] = [1.0]

    return {
        "daily_pnl": leg_df,
        "cumulative_pnl": cumulative,
        "metrics": metrics,
        "contribution": contribution,
    }


def statistical_arbitrage_with_leg_analytics(
    prices: pd.DataFrame,
    rolled_df: pd.DataFrame,
    leg1_daily_pnl: pd.Series,
    leg2_daily_pnl: pd.Series,
    zscore: pd.Series | None = None,
    spread_col: str | None = None,
    entry_z: float = 1.0,
    exit_z: float = 0.0,
    leg1_name: str = "leg_1",
    leg2_name: str = "leg_2",
    leg1_weight: float = 1.0,
    leg2_weight: float = -1.0,
    annualization: int = 252,
) -> dict[str, pd.DataFrame]:
    """
    Convenience wrapper:
    1. builds MTM-ready stat-arb path
    2. builds leg-level PnL attribution
    3. computes analytics for both legs and total strategy
    """
    strategy_df = statistical_arbitrage(
        prices=prices,
        rolled_df=rolled_df,
        zscore=zscore,
        spread_col=spread_col,
        entry_z=entry_z,
        exit_z=exit_z,
    )

    analytics = stat_arb_leg_analytics(
        strategy_df=strategy_df,
        leg1_daily_pnl=leg1_daily_pnl,
        leg2_daily_pnl=leg2_daily_pnl,
        leg1_name=leg1_name,
        leg2_name=leg2_name,
        leg1_weight=leg1_weight,
        leg2_weight=leg2_weight,
        annualization=annualization,
    )

    return {
        "strategy": strategy_df,
        "leg_daily_pnl": analytics["daily_pnl"],
        "leg_cumulative_pnl": analytics["cumulative_pnl"],
        "leg_metrics": analytics["metrics"],
        "leg_contribution": analytics["contribution"],
    }
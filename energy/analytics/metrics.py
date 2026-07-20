import numpy as np
import pandas as pd


def legacy_capstone_metrics(df: pd.DataFrame, contracts=1, units=1000) -> pd.Series:
    """
    Legacy capstone metrics for unit / quote-space strategy outputs.

    Expected columns:
        - net_pnl
        - t_cost
    """
    if "net_pnl" not in df.columns or "t_cost" not in df.columns:
        raise ValueError("DataFrame must include 'net_pnl' and 't_cost'.")

    _nan_metrics_ps = pd.Series({
        "Total PnL": np.nan, "Total Cost": np.nan, "APL/unit (ann.)": np.nan,
        "CAGR": np.nan, "Std Dev (ann.)": np.nan, "Sharpe": np.nan,
        "Drawdown": np.nan, "RoD": np.nan, "Years": np.nan,
    })

    if len(df) < 2:
        return _nan_metrics_ps

    idx = df.index
    years = (idx[-1] - idx[0]).days / 365.0
    obs_per_year = len(idx) / years if years > 0 else np.nan
    sqrt_af = np.sqrt(obs_per_year) if obs_per_year > 0 else np.nan

    total_pnl = df["net_pnl"].sum() * contracts * units
    total_cost = df["t_cost"].sum() * contracts * units

    apl_unit = (total_pnl / (contracts * units)) / years if years > 0 else np.nan

    daily_std = df["net_pnl"].std()
    ann_std = daily_std * sqrt_af if pd.notna(sqrt_af) else np.nan
    sharpe = apl_unit / ann_std if pd.notna(ann_std) and ann_std != 0 else np.nan

    pnl_index = 100.0 + df["net_pnl"].cumsum()
    start_val = pnl_index.iloc[0]
    end_val = pnl_index.iloc[-1]

    if years > 0 and start_val > 0 and end_val > 0:
        cagr = (end_val / start_val) ** (1.0 / years) - 1.0
    else:
        cagr = np.nan

    roll_max = pnl_index.cummax()
    drawdown = (pnl_index / roll_max - 1.0).min()
    rod = -apl_unit / drawdown if pd.notna(drawdown) and drawdown != 0 else np.nan

    out = pd.Series({
        "Total PnL": round(total_pnl, 4),
        "Total Cost": round(-total_cost, 4),
        "APL/unit (ann.)": round(apl_unit, 4),
        "CAGR": round(cagr, 4),
        "Std Dev (ann.)": round(ann_std, 4),
        "Sharpe": round(sharpe, 4),
        "Drawdown": round(drawdown, 4),
        "RoD": round(rod, 4),
        "Years": round(years, 4),
    })

    pd.options.display.float_format = "{:,.4f}".format
    return out


def metrics(df: pd.DataFrame) -> pd.Series:
    """
    MTM metrics for true capital-account outputs.

    Expected columns:
        - capital
    Optional helpful columns:
        - dollar_pnl
        - txn_cost_mtm
        - equity_index
    """
    if "capital" not in df.columns:
        raise ValueError("DataFrame must include 'capital'.")

    if len(df) < 2:
        raise ValueError("DataFrame must contain at least 2 rows.")

    capital = df["capital"].astype(float)

    _nan_metrics = pd.Series({
        "Total PnL": np.nan, "Total Cost": np.nan, "APL $ (ann.)": np.nan,
        "CAGR": np.nan, "Ann Return": np.nan, "Std Dev (ann.)": np.nan,
        "Sharpe": np.nan, "Drawdown": np.nan, "RoD": np.nan, "Years": np.nan,
    })

    if capital.isna().all():
        return _nan_metrics

    ret = capital.pct_change().dropna()
    if len(ret) == 0:
        return _nan_metrics

    idx = df.index
    years = (idx[-1] - idx[0]).days / 365.0
    obs_per_year = len(ret) / years if years > 0 else np.nan
    sqrt_af = np.sqrt(obs_per_year) if pd.notna(obs_per_year) and obs_per_year > 0 else np.nan

    start_cap = capital.iloc[0]
    end_cap = capital.iloc[-1]

    total_pnl = end_cap - start_cap

    if "txn_cost_mtm" in df.columns:
        total_cost = df["txn_cost_mtm"].sum()
    else:
        total_cost = np.nan

    apl_dollar = total_pnl / years if years > 0 else np.nan

    mean_daily = ret.mean()
    daily_std = ret.std()
    ann_return = mean_daily * obs_per_year if pd.notna(obs_per_year) else np.nan
    ann_std = daily_std * sqrt_af if pd.notna(sqrt_af) else np.nan
    sharpe = ann_return / ann_std if pd.notna(ann_std) and ann_std != 0 else np.nan

    if years > 0 and start_cap > 0 and end_cap > 0:
        cagr = (end_cap / start_cap) ** (1.0 / years) - 1.0
    else:
        cagr = np.nan

    roll_max = capital.cummax()
    drawdown = (capital / roll_max - 1.0).min()
    rod = -cagr / drawdown if pd.notna(drawdown) and drawdown != 0 else np.nan

    out = pd.Series({
        "Total PnL": round(total_pnl, 4),
        "Total Cost": round(total_cost, 4) if pd.notna(total_cost) else np.nan,
        "APL $ (ann.)": round(apl_dollar, 4),
        "CAGR": round(cagr, 4),
        "Ann Return": round(ann_return, 4),
        "Std Dev (ann.)": round(ann_std, 4),
        "Sharpe": round(sharpe, 4),
        "Drawdown": round(drawdown, 4),
        "RoD": round(rod, 4),
        "Years": round(years, 4),
    })

    pd.options.display.float_format = "{:,.4f}".format
    return out
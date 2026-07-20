"""
rv_param_sweep.py
-----------------
Parameter sensitivity sweep for RV stat-arb strategies.

Runs a strategy over a grid of (lookback, threshold/band) values and returns
a tidy metrics DataFrame. Works with both pct-deviation and z-score strategies.

Public API
----------
param_sweep(...)                 -> pd.DataFrame   (one row per param combo)
cross_pair_sweep(...)            -> dict            (per_pair + mean + median)
plot_sweep_heatmap(...)          -> matplotlib Figure
plot_cross_pair_heatmap(...)     -> matplotlib Figure
"""

import itertools
import warnings

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import matplotlib.colors as mcolors
import numpy as np
import pandas as pd

from energy.strategies.relative_value.rv_pct_deviation import rv_pct_deviation
from energy.strategies.relative_value.rv_zscore import rv_zscore
from energy.strategies.relative_value.rv_percentile import rv_percentile


# ============================================================
# METRICS HELPER
# ============================================================
def _strategy_metrics(pack: dict) -> dict:
    df = pack["strategy_df"]
    ret = df["daily_ret"].fillna(0.0)

    equity    = df["equity_index"].ffill()
    total_ret = float(equity.iloc[-1] - 1.0) if not equity.empty else np.nan
    ann_ret   = float(ret.mean() * 252)
    ann_vol   = float(ret.std(ddof=0) * np.sqrt(252))
    sharpe    = np.nan if ann_vol == 0 else ann_ret / ann_vol

    hwm    = equity.cummax()
    dd     = equity / hwm - 1.0
    max_dd = float(dd.min())
    rod    = np.nan if max_dd == 0 else total_ret / abs(max_dd)

    sig        = df["signal_raw"]
    n_entries  = int((sig.diff().abs() > 0).sum())
    pct_in     = float((sig != 0).mean())

    return {
        "Sharpe":        round(sharpe,    3),
        "Total Return":  round(total_ret, 4),
        "Ann Return":    round(ann_ret,   4),
        "Ann Vol":       round(ann_vol,   4),
        "Max DD":        round(max_dd,    4),
        "RoD":           round(rod,       3) if not np.isnan(rod) else np.nan,
        "Entries":       n_entries,
        "Pct In Trade":  round(pct_in,    3),
    }


# ============================================================
# SINGLE-PAIR SWEEP
# ============================================================
def param_sweep(
    leg1_name: str,
    leg2_name: str,
    prices1: pd.DataFrame,
    prices2: pd.DataFrame,
    expiry1: pd.Series,
    expiry2: pd.Series,
    lookbacks: list[int],
    thresholds: list[float],
    initial_capital: float = 1_000_000.0,
    strategy: str = "pct_deviation",
    vol_window: int = 0,
    trade_start: str | None = None,
    roll_config: str = "prompt_EOM_roll",
    signal_roll_config: str | None = None,
) -> pd.DataFrame:
    """
    Sweep over (lookback, threshold) for a single pair.

    Parameters
    ----------
    strategy : "pct_deviation" | "zscore" | "percentile"
    thresholds : deviation band pct (pct_deviation), z-score threshold (zscore),
                 or entry percentile tail (percentile, e.g. 0.10 = bottom/top 10%)
    trade_start : ISO date string — signal warms up on full history, capital
                  loop starts at this date (equity index = 1.0 at trade_start).
    signal_roll_config : if provided, signal is computed from this roll config
                         while execution uses roll_config (split-tenor strategy).

    Returns
    -------
    pd.DataFrame with MultiIndex (lookback, threshold) and metric columns.
    """
    if strategy == "pct_deviation":
        fn, kw_key = rv_pct_deviation, "deviation_band_pct"
    elif strategy == "zscore":
        fn, kw_key = rv_zscore, "zscore_threshold"
    elif strategy == "percentile":
        fn, kw_key = rv_percentile, "entry_pct"
    else:
        raise ValueError(f"Unknown strategy '{strategy}'. Choose pct_deviation, zscore, or percentile.")

    rows = []
    for lb, thr in itertools.product(lookbacks, thresholds):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                pack = fn(
                    leg1_name=leg1_name, leg2_name=leg2_name,
                    prices1=prices1, prices2=prices2,
                    expiry1=expiry1, expiry2=expiry2,
                    initial_capital=initial_capital,
                    lookback=lb,
                    vol_window=vol_window,
                    trade_start=trade_start,
                    roll_config=roll_config,
                    signal_roll_config=signal_roll_config,
                    **{kw_key: thr},
                )
            m = _strategy_metrics(pack)
        except Exception:
            m = {k: np.nan for k in ["Sharpe", "Total Return", "Ann Return",
                                     "Ann Vol", "Max DD", "RoD", "Entries", "Pct In Trade"]}
        m["lookback"]   = lb
        m["threshold"]  = thr
        rows.append(m)

    df = pd.DataFrame(rows).set_index(["lookback", "threshold"])
    return df


# ============================================================
# CROSS-PAIR SWEEP
# ============================================================
def cross_pair_sweep(
    pairs_data: dict,
    lookbacks: list[int],
    thresholds: list[float],
    initial_capital: float = 1_000_000.0,
    strategy: str = "pct_deviation",
    vol_window: int = 0,
    trade_start: str | None = None,
    verbose: bool = True,
    roll_config: str = "prompt_EOM_roll",
    signal_roll_config: str | None = None,
) -> dict:
    """
    Run param_sweep for every pair and aggregate.

    Parameters
    ----------
    pairs_data : dict keyed by pair label e.g. "Propane vs Butane"
                 each value has keys: leg1_name, leg2_name, prices1, prices2,
                 expiry1, expiry2

    Returns
    -------
    dict with keys:
        "per_pair" : dict[label -> sweep DataFrame]
        "mean"     : DataFrame of mean metrics across pairs
        "median"   : DataFrame of median metrics across pairs
    """
    per_pair = {}
    for label, d in pairs_data.items():
        if verbose:
            print(f"  sweeping {label}...")
        try:
            per_pair[label] = param_sweep(
                leg1_name=d["leg1_name"], leg2_name=d["leg2_name"],
                prices1=d["prices1"],    prices2=d["prices2"],
                expiry1=d["expiry1"],    expiry2=d["expiry2"],
                lookbacks=lookbacks,     thresholds=thresholds,
                initial_capital=initial_capital,
                strategy=strategy,       vol_window=vol_window,
                trade_start=trade_start, roll_config=roll_config,
                signal_roll_config=signal_roll_config,
            )
        except Exception as e:
            if verbose:
                print(f"    FAIL {label}: {e}")

    if not per_pair:
        return {"per_pair": {}, "mean": pd.DataFrame(), "median": pd.DataFrame()}

    numeric_cols = ["Sharpe", "Total Return", "Ann Return", "Ann Vol",
                    "Max DD", "RoD", "Entries", "Pct In Trade"]
    stacked = pd.concat(
        [df[numeric_cols] for df in per_pair.values()],
        keys=per_pair.keys(),
        axis=0,
    )
    mean_df   = stacked.groupby(level=[1, 2]).mean()
    median_df = stacked.groupby(level=[1, 2]).median()

    return {"per_pair": per_pair, "mean": mean_df, "median": median_df}


# ============================================================
# PLOTTING
# ============================================================
def _pivot_metric(sweep_df: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Pivot sweep DataFrame to (lookback x threshold) matrix for heatmap."""
    return sweep_df[metric].unstack(level="threshold")


def plot_sweep_heatmap(
    sweep_df: pd.DataFrame,
    metrics: list[str] | None = None,
    title: str = "",
    figsize: tuple | None = None,
) -> plt.Figure:
    """
    Plot one heatmap per metric for a single-pair sweep DataFrame.

    Color convention (consistent across all metrics):
      Green  = good / positive
      Yellow = near zero / neutral
      Red    = bad / negative

    Parameters
    ----------
    metrics : list of column names to plot; defaults to Sharpe + Total Return + Max DD + Entries
    """
    if metrics is None:
        metrics = ["Sharpe", "Total Return", "Max DD", "Entries"]

    metrics = [m for m in metrics if m in sweep_df.columns]
    n = len(metrics)
    fs = figsize or (5 * n, 4)
    fig, axes = plt.subplots(1, n, figsize=fs)
    if n == 1:
        axes = [axes]

    for ax, metric in zip(axes, metrics):
        pivot = _pivot_metric(sweep_df, metric)
        vals  = pivot.values.astype(float)
        vmin, vmax = np.nanmin(vals), np.nanmax(vals)

        # Build norm and colormap so green=good, yellow=neutral, red=bad
        if metric == "Max DD":
            # Max DD is always ≤ 0; green = close to 0 (small DD), red = very negative
            norm = mcolors.Normalize(vmin=vmin, vmax=0)
            cmap = "RdYlGn"
        elif metric == "Entries":
            # Entries: informational count, no good/bad — use neutral blue gradient
            norm = mcolors.Normalize(vmin=vmin, vmax=vmax)
            cmap = "Blues"
        else:
            # Sharpe, Total Return, etc.: diverging around 0
            # green = positive, yellow = near zero, red = negative
            abs_max = max(abs(vmin), abs(vmax), 1e-9)
            if vmin >= 0:
                # All positive — still use RdYlGn but anchored at 0
                norm = mcolors.Normalize(vmin=0, vmax=vmax)
            elif vmax <= 0:
                # All negative — red range only
                norm = mcolors.Normalize(vmin=vmin, vmax=0)
            else:
                # Crosses zero — TwoSlopeNorm centers yellow exactly at 0
                norm = mcolors.TwoSlopeNorm(vmin=vmin, vcenter=0, vmax=vmax)
            cmap = "RdYlGn"

        im = ax.imshow(vals, aspect="auto", cmap=cmap, norm=norm,
                       interpolation="nearest")
        plt.colorbar(im, ax=ax, shrink=0.8)

        ax.set_xticks(range(len(pivot.columns)))
        # Format as % if values look like pct bands (< 0.5), else plain decimal (z-scores, counts)
        ax.set_xticklabels(
            [f"{v:.0%}" if metric not in ("Entries",) and v < 0.5
             else f"{v:.2f}" if v < 10
             else f"{v:.0f}"
             for v in pivot.columns],
            fontsize=7, rotation=45,
        )
        ax.set_yticks(range(len(pivot.index)))
        ax.set_yticklabels(pivot.index, fontsize=7)
        ax.set_xlabel("Threshold", fontsize=8)
        ax.set_ylabel("Lookback", fontsize=8)
        ax.set_title(metric, fontsize=9)

        # Annotate cells
        for ri in range(vals.shape[0]):
            for ci in range(vals.shape[1]):
                v = vals[ri, ci]
                if np.isnan(v):
                    continue
                txt = f"{v:.2f}" if abs(v) < 100 else f"{v:.0f}"
                ax.text(ci, ri, txt, ha="center", va="center", fontsize=6,
                        color="black")

    if title:
        fig.suptitle(title, fontsize=11, y=1.01)
    plt.tight_layout()
    return fig


def plot_cross_pair_heatmap(
    cross_sweep: dict,
    metrics: list[str] | None = None,
    agg: str = "mean",
    title: str = "",
    strategy: str = "pct_deviation",
    figsize: tuple | None = None,
) -> plt.Figure:
    """
    Plot aggregated (mean or median) heatmaps across all pairs.

    Parameters
    ----------
    cross_sweep : output of cross_pair_sweep()
    agg         : "mean" | "median"
    """
    agg_df = cross_sweep[agg]
    if agg_df.empty:
        raise ValueError("cross_sweep result is empty")

    if metrics is None:
        metrics = ["Sharpe", "Total Return", "Max DD", "Entries"]

    thr_label = {"pct_deviation": "Band %", "zscore": "Z-Threshold", "percentile": "Entry Pct"}.get(strategy, "Threshold")
    fig = plot_sweep_heatmap(
        agg_df,
        metrics=metrics,
        title=title or f"Cross-Pair {agg.title()} | {thr_label}",
    )
    return fig

"""
api/serialize.py — Convert pandas objects to JSON-friendly dicts.

Consistent formats used across all API responses:

  Time series (DatetimeIndex DataFrame):
      {"dates": ["2024-01-02", ...], "columns": {"F1": [72.5, ...], "F2": [...]}}

  Single series (DatetimeIndex Series):
      {"dates": ["2024-01-02", ...], "values": [72.5, ...]}

  Records (flat table):
      [{"col1": val1, "col2": val2}, ...]

  Heatmap grid (2D sweep):
      {"x": [...], "y": [...], "z": [[...], ...], "x_title": "...", ...}

  Metrics:
      {"Sharpe": 0.85, "CAGR": 0.12, ...}

NaN / Inf / NaT → null in all cases.
"""

import math
import numpy as np
import pandas as pd


def _clean(v):
    """Coerce a single numpy/pandas scalar to a JSON-safe Python type."""
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, (pd.Timestamp, np.datetime64)):
        ts = pd.Timestamp(v)
        return ts.isoformat()[:10] if not pd.isna(ts) else None
    return v


def _clean_deep(v):
    """Recursively apply _clean through nested dicts/lists (e.g. spread_construction,
    whose bloomberg_check sub-dict carries raw numpy/NaN values from validate_against_listed_spread)."""
    if isinstance(v, dict):
        return {k: _clean_deep(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean_deep(x) for x in v]
    return _clean(v)


# ── time series ──────────────────────────────────────────────────────────────

def series_to_dict(s: pd.Series) -> dict:
    """DatetimeIndex Series → {"dates": [...], "values": [...]}."""
    return {
        "dates": [d.isoformat()[:10] for d in s.index],
        "values": [_clean(v) for v in s.values],
    }


def df_to_timeseries(df: pd.DataFrame) -> dict:
    """DatetimeIndex DataFrame → column-oriented JSON."""
    return {
        "dates": [d.isoformat()[:10] for d in df.index],
        "columns": {
            str(col): [_clean(v) for v in df[col].values]
            for col in df.columns
        },
    }


# ── flat tables ──────────────────────────────────────────────────────────────

def df_to_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame → list of row dicts."""
    records = []
    for _, row in df.iterrows():
        records.append({str(k): _clean(v) for k, v in row.items()})
    return records


# ── scalar dicts ─────────────────────────────────────────────────────────────

def metrics_to_dict(m: dict) -> dict:
    """Clean metric dict values for JSON serialization."""
    return {str(k): _clean(v) for k, v in m.items()}


# ── 2-D grids (parameter sweeps) ────────────────────────────────────────────

def grid_to_heatmap(grid: pd.DataFrame, info: dict) -> dict:
    """
    Sweep grid DataFrame → Plotly-ready heatmap payload.

    grid: DataFrame with numeric index (y-axis) and numeric columns (x-axis).
    info: dict with x_title, y_title, cur_x, cur_y, title from lab.sweep_for().
    """
    x = [_clean(c) for c in grid.columns]
    y = [_clean(i) for i in grid.index]
    z = [[_clean(grid.iloc[r, c]) for c in range(len(x))] for r in range(len(y))]
    return {
        "x": x,
        "y": y,
        "z": z,
        "x_title": info.get("x_title", ""),
        "y_title": info.get("y_title", ""),
        "cur_x": _clean(info.get("cur_x")),
        "cur_y": _clean(info.get("cur_y")),
        "title": info.get("title", ""),
    }


# ── lab result (complex dict with mixed pandas/scalar content) ───────────────

def serialize_lab_result(key: str, result: dict) -> dict:
    """Convert a lab result dict (pandas-heavy) to a fully JSON-safe dict."""
    out = {
        "key": key,
        "kind": result["kind"],
        "strategy": result["strategy"],
        "commodity": result["commodity"],
        "label": result["label"],
        "price_space": df_to_timeseries(result["price_space"]),
        "price_space_metrics": metrics_to_dict(result["price_space_metrics"]),
        "mtm": df_to_timeseries(result["mtm"]),
        "mtm_metrics": metrics_to_dict(result["mtm_metrics"]),
        "position": series_to_dict(result["position"]),
    }

    if result["kind"] == "directional":
        out["held_price_native"] = series_to_dict(result["held_price_native"])
        out["norm_scale"] = _clean(result.get("norm_scale"))

    if result["kind"] == "pair":
        out["leg1"] = result.get("leg1")
        out["leg2"] = result.get("leg2")
        out["spread"] = df_to_timeseries(result["spread"])
        out["entry_threshold"] = _clean(result.get("entry_threshold"))
        out["exit_threshold"] = _clean(result.get("exit_threshold"))
        out["spread_construction"] = _clean_deep(result.get("spread_construction"))

    if result.get("cot") is not None:
        cot_df = result["cot"]
        if "date" in cot_df.columns and not isinstance(cot_df.index, pd.DatetimeIndex):
            cot_df = cot_df.set_index("date")
        out["cot"] = df_to_timeseries(cot_df)
        out["cot_signal_df"] = df_to_timeseries(result["cot_signal_df"])
        out["cot_mode"] = result.get("cot_mode")
        out["cot_synthetic"] = result.get("cot_synthetic")

    return out

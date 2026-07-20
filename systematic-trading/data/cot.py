"""
data/cot.py — COT (Commitment of Traders) data layer.

Interface contract (matches the planned cot_bbg table):
    get_cot(commodity) -> DataFrame with columns:
        date, commodity, mm_long, mm_short, mm_net, mm_net_change,
        percentile_rank, crowding_flag

Data source is currently SYNTHETIC (numpy RNG, seeded per commodity) because
the cot_bbg table is not built yet.  All consumers go through get_cot(), so
when real data lands the only change needed is _fetch_cot_bbg() below —
the signal helpers and every page keep working unchanged.

Signal helpers (used by Strategy Lab and the COT Flows page):
    follow_the_flow(cot_df, fast, slow)   — MA crossover on MM net position
    fade_the_crowd(cot_df, threshold_pct) — sentiment-index contrarian signal
    weekly_to_daily_position(...)         — Tuesday snapshot -> tradeable daily series
"""

import pandas as pd
import numpy as np

# CFTC timing: Tuesday snapshot -> Friday publish -> Monday execution.
# Shifting the snapshot date forward 6 calendar days lands on the first
# day the information is actually tradeable.
PUBLICATION_LAG_DAYS = 6

_HIST_START = "2015-01-01"

_SEED_MAP = {
    "WTI": 1, "Brent": 2, "Natgas": 3, "Nat Gas": 3,
    "RBOB": 4, "ULSD": 5, "Gasoil": 6,
    "Propane": 7, "Ethane": 8, "Butane": 9,
}


# ---------------------------------------------------------------------------
# Source — swap point for the real cot_bbg table
# ---------------------------------------------------------------------------

def _fetch_cot_bbg(commodity: str, start_date: str, end_date: str) -> pd.DataFrame | None:
    """
    Pull raw COT rows from the cot_bbg table once it exists.

    Return None to signal "table not available" — callers fall back to the
    synthetic generator. When the table lands, implement with data.db.query_df
    and return columns: date, commodity, mm_long, mm_short.
    """
    return None  # cot_bbg not built yet


def _synthetic_cot(commodity: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Seeded random-walk MM positioning — stable across calls per commodity."""
    rng = np.random.default_rng(_SEED_MAP.get(commodity, 99))
    dates = pd.date_range(start_date, end_date, freq="W-TUE")

    mm_long = 250_000 + rng.normal(0, 12_000, len(dates)).cumsum()
    mm_short = 80_000 + rng.normal(0, 7_000, len(dates)).cumsum()
    return pd.DataFrame({
        "date": dates,
        "commodity": commodity,
        "mm_long": mm_long.astype(int),
        "mm_short": mm_short.astype(int),
    })


def _derive_fields(df: pd.DataFrame) -> pd.DataFrame:
    """Derived columns shared by synthetic and real sources."""
    out = df.copy()
    out["mm_net"] = out["mm_long"] - out["mm_short"]
    out["mm_net_change"] = out["mm_net"].diff().fillna(0).astype(int)

    pct = out["mm_net"].rolling(52, min_periods=1).rank(pct=True) * 100
    out["percentile_rank"] = pct.round(1)
    out["crowding_flag"] = pct.apply(
        lambda x: "Crowded" if x > 75 else ("Washed" if x < 25 else "Neutral")
    )
    return out


def get_cot(commodity: str, start_date: str = _HIST_START, end_date: str | None = None) -> pd.DataFrame:
    """
    COT positioning history for a commodity.

    Columns: date, commodity, mm_long, mm_short, mm_net, mm_net_change,
             percentile_rank, crowding_flag.
    Currently synthetic — see module docstring.
    """
    end_date = end_date or pd.Timestamp.today().strftime("%Y-%m-%d")
    raw = _fetch_cot_bbg(commodity, start_date, end_date)
    if raw is None:
        raw = _synthetic_cot(commodity, start_date, end_date)
    return _derive_fields(raw)


def is_synthetic() -> bool:
    """True while cot_bbg is not wired — pages show a placeholder notice."""
    return _fetch_cot_bbg("WTI", _HIST_START, _HIST_START) is None


def get_cot_snapshot(commodities: list[str] | None = None) -> pd.DataFrame:
    """Latest COT row per commodity — for the positioning table."""
    commodities = commodities or ["WTI", "Brent", "Natgas"]
    frames = [get_cot(c).tail(1) for c in commodities]
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def follow_the_flow(cot_df: pd.DataFrame, fast: int = 4, slow: int = 16) -> pd.DataFrame:
    """
    Signal 1 — Follow the Flow: π = sign(MA(Net, fast) − MA(Net, slow)).

    fast/slow are in WEEKS (COT is weekly data).
    Returns a date-indexed DataFrame: mm_net, ma_fast, ma_slow, signal {-1,0,+1}.
    """
    net = cot_df.set_index("date")["mm_net"].astype(float)
    ma_f = net.rolling(fast, min_periods=fast).mean()
    ma_s = net.rolling(slow, min_periods=slow).mean()
    signal = pd.Series(np.sign(ma_f - ma_s), index=net.index).fillna(0.0)
    return pd.DataFrame({"mm_net": net, "ma_fast": ma_f, "ma_slow": ma_s, "signal": signal})


def fade_the_crowd(cot_df: pd.DataFrame, threshold_pct: float = 20.0, window: int = 52) -> pd.DataFrame:
    """
    Signal 2 — Fade the Crowd: SI = (Net − min) / (max − min) over `window` weeks.

    Buy (+1) when SI < threshold_pct, sell (−1) when SI > 100 − threshold_pct,
    flat between the bands.
    Returns a date-indexed DataFrame: mm_net, sentiment_index (0–100), signal.
    """
    net = cot_df.set_index("date")["mm_net"].astype(float)
    lo = net.rolling(window, min_periods=window // 2).min()
    hi = net.rolling(window, min_periods=window // 2).max()
    rng = (hi - lo).replace(0, np.nan)
    si = ((net - lo) / rng * 100).rename("sentiment_index")

    signal = pd.Series(0.0, index=net.index)
    signal[si < threshold_pct] = 1.0
    signal[si > 100 - threshold_pct] = -1.0
    signal[si.isna()] = 0.0
    return pd.DataFrame({"mm_net": net, "sentiment_index": si, "signal": signal})


def weekly_to_daily_position(
    weekly_signal: pd.Series,
    daily_index: pd.DatetimeIndex,
    lag_days: int = PUBLICATION_LAG_DAYS,
) -> pd.Series:
    """
    Convert a Tuesday-stamped weekly signal into a daily tradeable position.

    The snapshot date is shifted forward by the publication lag (Tuesday data
    is only actionable the following Monday), then forward-filled onto the
    trading calendar. No additional execution lag is applied — the calendar
    shift already embodies it.
    """
    eff = weekly_signal.copy()
    eff.index = eff.index + pd.Timedelta(days=lag_days)
    eff = eff[~eff.index.duplicated(keep="last")].sort_index()
    return eff.reindex(daily_index, method="ffill").fillna(0.0).rename("position")

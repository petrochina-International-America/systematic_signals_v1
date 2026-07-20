"""
data/prices.py — Price accessors for the dashboard.

All data is served from data.loader (the in-memory cache populated at startup).
No direct DB queries here.
"""
import pandas as pd


def get_prices_wide(
    commodity:  str,
    start_date: str | None = None,
    end_date:   str | None = None,
) -> pd.DataFrame:
    """
    Full forward curve (F1..F24, date-indexed) for a commodity.

    Applies optional date filters to the in-memory cache.
    Returns an empty DataFrame if the commodity has no data.
    """
    from data import loader
    try:
        df = loader.get_prices(commodity)
    except (KeyError, RuntimeError):
        return pd.DataFrame()

    if start_date:
        df = df[df.index >= pd.Timestamp(start_date)]
    if end_date:
        df = df[df.index <= pd.Timestamp(end_date)]
    return df


def get_prices(commodity: str, start_date: str = "", end_date: str = "") -> pd.DataFrame:
    """
    Front-month (F1) price series for charts and overlays.

    Returns a DataFrame with columns: date, close, commodity.
    Returns an empty DataFrame (same shape) if the commodity has no data.
    """
    try:
        wide = get_prices_wide(commodity, start_date or None, end_date or None)
        if wide.empty or "F1" not in wide.columns:
            raise ValueError("no F1 data")
        df = wide[["F1"]].rename(columns={"F1": "close"}).reset_index()
        df["commodity"] = commodity
        return df[["date", "close", "commodity"]]
    except Exception:
        return pd.DataFrame({"date": [], "close": [], "commodity": []})

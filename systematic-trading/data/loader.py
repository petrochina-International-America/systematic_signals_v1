"""
data/loader.py — In-memory data store for the SystematicTrading dashboard.

Pulls all commodity price curves from FlowsDB in one batch at startup, then
serves them instantly to the energy library without further DB queries.

Lifecycle:
    warm_up()         — call once at app startup; raises loudly on any failure
    get_prices(name)  — returns cached wide DataFrame; triggers TTL refresh if stale
    get_expiry(root)  — returns expiry DatetimeIndex from the configured calendar file
    loaded_commodities() — list of names successfully pulled from FlowsDB
"""

import os
import re
import time
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

_HIST_START  = "2010-01-01"
_REFRESH_TTL = 4 * 3600  # seconds — re-pull from DB if data is older than 4 hours

# ── module-level store ────────────────────────────────────────────────────────
_prices:    dict[str, pd.DataFrame]     = {}   # commodity_name → wide F1..F24 df
_months:    dict[str, pd.DataFrame]     = {}   # commodity_name → wide F1..F24 CONTRACT_MONTH_YR
_expiry:    dict[str, pd.DatetimeIndex] = {}   # ticker_root    → DatetimeIndex
_loaded_at: float = 0.0


# ── internal helpers ──────────────────────────────────────────────────────────

def _specs() -> dict:
    from energy.accounting.contract_specs import CONTRACT_SPECS
    return CONTRACT_SPECS


def _all_tickers(specs: dict) -> list[str]:
    roots = {spec["ticker"] for spec in specs.values()}
    return [f"{root}{n} Comdty" for root in sorted(roots) for n in range(1, 25)]


def _fetch_prices(specs: dict) -> dict[str, pd.DataFrame]:
    """Single batch query to prices_daily → {commodity_name: wide F1..F24 DataFrame}."""
    from data.db import query_df
    from energy.preprocess.drop_dupes import drop_dupes

    tickers = _all_tickers(specs)
    sql = """
        SELECT date, bbg_ticker, value
        FROM prices_daily
        WHERE bbg_ticker = ANY(:tickers)
          AND field = 'PX_SETTLE'
          AND price_type = 'flat'
          AND date >= :start_date
          AND value IS NOT NULL
        ORDER BY date, bbg_ticker
    """
    df = query_df(sql, {"tickers": tickers, "start_date": _HIST_START})
    if df.empty:
        raise RuntimeError(
            "prices_daily returned no rows — FlowsDB unreachable or table empty."
        )

    df["date"] = pd.to_datetime(df["date"])

    root_to_name = {spec["ticker"]: name for name, spec in specs.items()}

    def _root(t: str) -> str | None:
        m = re.match(r"^([A-Z]+)\d+\s+Comdty$", t)
        return m.group(1) if m else None

    def _tenor(t: str) -> int | None:
        m = re.search(r"(\d+)\s+Comdty$", t)
        return int(m.group(1)) if m else None

    df["root"]      = df["bbg_ticker"].map(_root)
    df["n"]         = df["bbg_ticker"].map(_tenor)
    df["commodity"] = df["root"].map(root_to_name)
    df = df.dropna(subset=["commodity", "n"])
    df["col"] = "F" + df["n"].astype(int).astype(str)

    store: dict[str, pd.DataFrame] = {}
    for commodity, grp in df.groupby("commodity"):
        wide = grp.pivot_table(
            index="date", columns="col", values="value", aggfunc="first"
        )
        cols = sorted(
            [c for c in wide.columns if re.fullmatch(r"F\d+", c)],
            key=lambda x: int(x[1:]),
        )
        clean = drop_dupes(wide[cols])
        # Forward-fill single-day gaps (holidays where one exchange didn't trade)
        store[str(commodity)] = clean.ffill(limit=3)

    return store


def _fetch_months(specs: dict) -> dict[str, pd.DataFrame]:
    """
    Batch query of CONTRACT_MONTH_YR → {commodity_name: wide F1..F24 frame of
    YYYYMM delivery months}. Same long-format prices_daily table as prices —
    the delivery month lives as a `field` value per generic ticker, so this is
    a filter + pivot, not a join. Used by the delivery-month-matched spread
    engine (energy.strategies.spread_rolling); never normalized or scaled.
    """
    from data.db import query_df

    tickers = _all_tickers(specs)
    sql = """
        SELECT date, bbg_ticker, value
        FROM prices_daily
        WHERE bbg_ticker = ANY(:tickers)
          AND field = 'CONTRACT_MONTH_YR'
          AND price_type = 'flat'
          AND date >= :start_date
          AND value IS NOT NULL
        ORDER BY date, bbg_ticker
    """
    df = query_df(sql, {"tickers": tickers, "start_date": _HIST_START})
    if df.empty:
        return {}

    df["date"] = pd.to_datetime(df["date"])
    root_to_name = {spec["ticker"]: name for name, spec in specs.items()}

    def _root(t: str) -> str | None:
        m = re.match(r"^([A-Z]+)\d+\s+Comdty$", t)
        return m.group(1) if m else None

    def _tenor(t: str) -> int | None:
        m = re.search(r"(\d+)\s+Comdty$", t)
        return int(m.group(1)) if m else None

    df["root"]      = df["bbg_ticker"].map(_root)
    df["n"]         = df["bbg_ticker"].map(_tenor)
    df["commodity"] = df["root"].map(root_to_name)
    df = df.dropna(subset=["commodity", "n"])
    df["col"] = "F" + df["n"].astype(int).astype(str)

    store: dict[str, pd.DataFrame] = {}
    for commodity, grp in df.groupby("commodity"):
        wide = grp.pivot_table(
            index="date", columns="col", values="value", aggfunc="first"
        )
        cols = sorted(
            [c for c in wide.columns if re.fullmatch(r"F\d+", c)],
            key=lambda x: int(x[1:]),
        )
        # months are constant within a contract's life; short ffill only
        # bridges holiday gaps (same limit as prices)
        store[str(commodity)] = wide[cols].ffill(limit=3)
    return store


def _fetch_expiry(specs: dict, cal_path: str) -> dict[str, pd.DatetimeIndex]:
    """Load expiry calendars from the xlsx for all known ticker roots."""
    from energy.preprocess.expiry_calendar import expiry_calendar

    expiry: dict[str, pd.DatetimeIndex] = {}
    for spec in specs.values():
        root = spec.get("ticker")
        if not root or root in expiry:
            continue
        try:
            expiry[root] = expiry_calendar(root, cal_path)
        except Exception:
            pass  # ticker not in xlsx yet — callers handle missing expiry gracefully
    return expiry


# ── public API ────────────────────────────────────────────────────────────────

def warm_up() -> None:
    """
    Pull commodity prices and expiry calendars into the in-memory store.

    Must be called once at app startup. Raises on any failure so the process
    does not silently serve stale or empty data.
    """
    global _prices, _months, _expiry, _loaded_at

    specs    = _specs()
    cal_path = os.getenv(
        "EXPIRY_CALENDAR_PATH",
        "../Systematic_Energy_Trading-main/data/expiry_calendars.xlsx",
    )

    _prices    = _fetch_prices(specs)   # raises on DB failure
    _months    = _fetch_months(specs)
    _expiry    = _fetch_expiry(specs, cal_path)
    _loaded_at = time.time()


def _maybe_refresh() -> None:
    if _loaded_at > 0 and time.time() - _loaded_at > _REFRESH_TTL:
        warm_up()


def get_prices(commodity: str) -> pd.DataFrame:
    """
    Cached wide DataFrame (F1..F24, date-indexed) for a commodity.

    Refreshes from DB automatically if data is older than _REFRESH_TTL.
    Raises RuntimeError if the store hasn't been warmed up yet.
    Raises KeyError if the commodity has no data in FlowsDB.
    """
    _maybe_refresh()
    if not _prices:
        raise RuntimeError("data.loader not warmed up — call loader.warm_up() at startup.")
    if commodity not in _prices:
        raise KeyError(f"No price data in store for '{commodity}'.")
    return _prices[commodity]


def get_contract_months(commodity: str) -> pd.DataFrame:
    """
    Cached wide DataFrame (F1..F24, date-indexed) of each generic's delivery
    month as YYYYMM (Bloomberg CONTRACT_MONTH_YR). Required by the
    delivery-month-matched spread construction for strict_delivery_match
    pairs (see energy.accounting.spread_specs).

    Raises RuntimeError if the store hasn't been warmed up, KeyError if the
    commodity has no contract-month data.
    """
    _maybe_refresh()
    if not _prices:
        raise RuntimeError("data.loader not warmed up — call loader.warm_up() at startup.")
    if commodity not in _months:
        raise KeyError(f"No contract-month data in store for '{commodity}'.")
    return _months[commodity]


def get_expiry(ticker_root: str) -> pd.DatetimeIndex:
    """
    Expiry DatetimeIndex for a ticker root (e.g. 'CL' for WTI).

    Raises KeyError if the ticker is not in the loaded calendar file.
    """
    _maybe_refresh()
    if ticker_root not in _expiry:
        raise KeyError(f"No expiry calendar loaded for ticker '{ticker_root}'.")
    return _expiry[ticker_root]


def get_listed_spread(validate_ticker: str, rank: str = "1-1") -> tuple[pd.Series, pd.Series]:
    """
    Independent listed cross-commodity spread series (e.g. Bloomberg
    'S:ENCO 1-1 Comdty') as (settle_price, delivery_month) Series — for
    validating a leg-matched construction against a source that never goes
    through this app's roll machinery. See
    energy.strategies.spread_rolling.validate_against_listed_spread and
    SPREAD_SPECS[...]['validate_ticker'].

    Not cached/warmed-up with the rest of the store (used for on-demand
    diagnostics, not the strategy engines); queries FlowsDB directly.
    Raises KeyError if the ticker has no rows.
    """
    from data.db import query_df

    full_ticker = f"{validate_ticker} {rank} Comdty"
    df = query_df(
        """
        SELECT date, value, field FROM prices_daily
        WHERE bbg_ticker = :ticker
          AND price_type = 'spread'
          AND field IN ('PX_SETTLE', 'CONTRACT_MONTH_YR')
          AND value IS NOT NULL
        ORDER BY date
        """,
        {"ticker": full_ticker},
    )
    if df.empty:
        raise KeyError(f"No data in prices_daily for listed spread '{full_ticker}'.")

    df["date"] = pd.to_datetime(df["date"])
    price = df[df["field"] == "PX_SETTLE"].set_index("date")["value"].sort_index()
    month = df[df["field"] == "CONTRACT_MONTH_YR"].set_index("date")["value"].sort_index()
    return price, month


def loaded_commodities() -> list[str]:
    """Commodity names successfully loaded from FlowsDB."""
    return sorted(_prices.keys())


def latest_data_date() -> str | None:
    """Most recent trading date across all loaded commodities (ISO string)."""
    if not _prices:
        return None
    latest = max(df.index.max() for df in _prices.values() if not df.empty)
    return latest.strftime("%Y-%m-%d")


def latest_pull_timestamp() -> str | None:
    """Most recent pull_timestamp from prices_daily (ISO string)."""
    try:
        from data.db import query_df
        df = query_df(
            "SELECT MAX(pull_timestamp) AS ts FROM prices_daily"
        )
        ts = df["ts"].iloc[0]
        if ts is not None:
            return str(ts)
    except Exception:
        pass
    return None


def loaded_at_iso() -> str | None:
    """Timestamp when the data store was last loaded (ISO string)."""
    if _loaded_at == 0.0:
        return None
    from datetime import datetime
    return datetime.fromtimestamp(_loaded_at).strftime("%Y-%m-%dT%H:%M:%S")


def get_prices_normalized(commodity: str) -> pd.DataFrame:
    """
    Wide F1..F24 DataFrame scaled by CONTRACT_SPECS[commodity]["normalization"].

    Strategy engines (build_roll_path/momentum/carry/build_measures) were
    calibrated on this normalized ($/bbl-equivalent) basis — use this instead
    of get_prices() when feeding prices into the energy strategy pipeline.
    get_prices() itself stays raw/native for display and cross-checks.
    """
    from energy.accounting.contract_specs import CONTRACT_SPECS

    scale = CONTRACT_SPECS.get(commodity, {}).get("normalization", 1.0) or 1.0
    return get_prices(commodity) * scale


def get_prices_aligned(commodities: list[str]) -> dict[str, pd.DataFrame]:
    """
    Wide F1..F24 DataFrames for multiple commodities, reindexed onto their
    shared intersection calendar (dates present for ALL requested commodities).

    Thin FlowsDB-sourcing wrapper around energy's `align_to_intersection`
    (see export_to_excel.py's `intersection_idx` step for the original pattern).
    """
    from energy.preprocess.loaders import align_to_intersection

    frames = {c: get_prices(c) for c in commodities}
    return align_to_intersection(frames)

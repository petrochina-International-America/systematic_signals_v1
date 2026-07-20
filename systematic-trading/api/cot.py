"""
/api/cot — COT (Commitment of Traders) positioning data and signals.

Data is currently synthetic (seeded random walk per commodity).
When the cot_bbg table lands, only _fetch_cot_bbg() changes —
these endpoints stay the same.
"""

from fastapi import APIRouter, Query

from api.serialize import df_to_timeseries, df_to_records, _clean

router = APIRouter()

_DEFAULT_START = "2015-01-01"
_DEFAULT_COMMODITIES = ["WTI", "Brent", "Natgas", "RBOB", "ULSD", "Gasoil"]


@router.get("/status")
def cot_status():
    """Whether COT data is still synthetic (cot_bbg table pending)."""
    from data.cot import is_synthetic
    return {"synthetic": is_synthetic()}


@router.get("/snapshot")
def cot_snapshot(
    commodities: str | None = Query(None, description="Comma-separated list, e.g. WTI,Brent,Natgas"),
):
    """Latest COT row per commodity — the positioning summary table."""
    from data.cot import get_cot_snapshot

    names = [c.strip() for c in commodities.split(",")] if commodities else _DEFAULT_COMMODITIES
    df = get_cot_snapshot(names)
    return {"data": df_to_records(df)}


@router.get("/{commodity}")
def get_cot(
    commodity: str,
    start_date: str = Query(_DEFAULT_START),
    end_date: str | None = Query(None),
):
    """
    Full COT positioning history for one commodity.

    Columns: mm_long, mm_short, mm_net, mm_net_change, percentile_rank, crowding_flag.
    """
    from data.cot import get_cot as _get_cot

    df = _get_cot(commodity, start_date, end_date)
    ts = df.set_index("date") if "date" in df.columns else df

    latest = df.iloc[-1]
    summary = {
        "mm_net": _clean(latest["mm_net"]),
        "mm_net_change": _clean(latest["mm_net_change"]),
        "percentile_rank": _clean(latest["percentile_rank"]),
        "crowding_flag": str(latest["crowding_flag"]),
    }

    return {
        "commodity": commodity,
        "latest": summary,
        "history": df_to_timeseries(ts),
    }


@router.get("/{commodity}/follow-the-flow")
def follow_the_flow(
    commodity: str,
    fast: int = Query(4, description="Fast MA window in weeks"),
    slow: int = Query(16, description="Slow MA window in weeks"),
    start_date: str = Query(_DEFAULT_START),
    end_date: str | None = Query(None),
):
    """
    Follow-the-Flow signal: sign(MA(net, fast) − MA(net, slow)).

    Returns mm_net, ma_fast, ma_slow, signal columns.
    """
    from data.cot import get_cot as _get_cot, follow_the_flow as _ftf

    cot_df = _get_cot(commodity, start_date, end_date)
    sig_df = _ftf(cot_df, fast=fast, slow=slow)
    return {
        "commodity": commodity,
        "signal_type": "follow_the_flow",
        "params": {"fast": fast, "slow": slow},
        "data": df_to_timeseries(sig_df),
    }


@router.get("/{commodity}/fade-the-crowd")
def fade_the_crowd(
    commodity: str,
    threshold_pct: float = Query(20.0, description="SI buy/sell threshold (0-50)"),
    start_date: str = Query(_DEFAULT_START),
    end_date: str | None = Query(None),
):
    """
    Fade-the-Crowd signal: contrarian sentiment index.

    Returns mm_net, sentiment_index, signal columns.
    """
    from data.cot import get_cot as _get_cot, fade_the_crowd as _ftc

    cot_df = _get_cot(commodity, start_date, end_date)
    sig_df = _ftc(cot_df, threshold_pct=threshold_pct)
    return {
        "commodity": commodity,
        "signal_type": "fade_the_crowd",
        "params": {"threshold_pct": threshold_pct},
        "data": df_to_timeseries(sig_df),
    }

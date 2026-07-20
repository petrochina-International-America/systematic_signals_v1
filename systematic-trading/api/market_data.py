"""
/api/market-data — Price curves and commodity metadata.

Data is served from the in-memory store (data.loader), which pulls from
FlowsDB once at startup and auto-refreshes every 4 hours.
"""

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from api.serialize import df_to_timeseries, df_to_records

router = APIRouter()


class AlignedRequest(BaseModel):
    commodities: list[str]


@router.get("/commodities")
def list_commodities():
    """All commodity names loaded from FlowsDB."""
    from data import loader
    return {"commodities": loader.loaded_commodities()}


@router.get("/prices/{commodity}")
def get_prices(
    commodity: str,
    start_date: str | None = Query(None, description="ISO date, e.g. 2020-01-01"),
    end_date: str | None = Query(None),
    normalized: bool = Query(False, description="Scale by normalization factor ($/bbl-equivalent)"),
):
    """
    Full forward curve (F1..F24) for one commodity.

    Returns column-oriented JSON: dates[] + one array per tenor column.
    """
    from data import loader

    try:
        df = loader.get_prices_normalized(commodity) if normalized else loader.get_prices(commodity)
    except KeyError:
        raise HTTPException(404, f"No price data for '{commodity}'")
    except RuntimeError as e:
        raise HTTPException(503, str(e))

    if start_date:
        df = df[df.index >= start_date]
    if end_date:
        df = df[df.index <= end_date]

    result = df_to_timeseries(df)
    result["commodity"] = commodity
    result["normalized"] = normalized
    return result


@router.get("/prices/{commodity}/front")
def get_front_month(
    commodity: str,
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
):
    """Front-month (F1) close series — [{date, close, commodity}, ...]."""
    from data.prices import get_prices

    df = get_prices(commodity, start_date or "", end_date or "")
    if df.empty:
        raise HTTPException(404, f"No front-month data for '{commodity}'")
    return {"commodity": commodity, "data": df_to_records(df)}


@router.post("/prices/aligned")
def get_prices_aligned(body: AlignedRequest):
    """
    Multiple commodities reindexed onto their shared trading calendar.

    POST body: {"commodities": ["WTI", "Brent"]}
    """
    from data import loader

    if not body.commodities:
        raise HTTPException(400, "Provide at least one commodity name")
    try:
        aligned = loader.get_prices_aligned(body.commodities)
    except (KeyError, RuntimeError) as e:
        raise HTTPException(400, str(e))

    return {
        name: {**df_to_timeseries(df), "commodity": name}
        for name, df in aligned.items()
    }

import pandas as pd


def construct_time_spread(
    prices: pd.DataFrame,
    near_tenor: str = "F1",
    far_tenor: str = "F2",
    *,
    commodity_name: str | None = None,
) -> pd.DataFrame:
    """
    Construct a calendar (time) spread for a single commodity:
        spread = prices[near_tenor] - prices[far_tenor]

    Parameters
    ----------
    prices : DataFrame
        DatetimeIndex, columns F1, F2, F3, ... (as returned by load_prices).
        Prices should already be normalized to a common unit ($/BBL etc.)
        before calling this function.
    near_tenor : str
        Column name for the near contract, e.g. "F1".
    far_tenor : str
        Column name for the far contract, e.g. "F2".
    commodity_name : str, optional
        Label override; falls back to prices.attrs["commodity_name"] if set.

    Returns
    -------
    DataFrame
        Columns: near_price, far_price, spread.
        Index: DatetimeIndex (dates where both tenors have non-NaN prices).
        Attributes: near_tenor, far_tenor, commodity_name, spread_type, label.
    """
    if near_tenor not in prices.columns:
        raise ValueError(
            f"near_tenor '{near_tenor}' not found. Available: {list(prices.columns)}"
        )
    if far_tenor not in prices.columns:
        raise ValueError(
            f"far_tenor '{far_tenor}' not found. Available: {list(prices.columns)}"
        )

    out = pd.DataFrame(
        {
            "near_price": prices[near_tenor],
            "far_price": prices[far_tenor],
        },
        index=prices.index,
    )
    out["spread"] = out["near_price"] - out["far_price"]
    out = out.dropna()

    name = commodity_name or prices.attrs.get("commodity_name", "unknown")
    out.attrs.update(
        {
            "near_tenor": near_tenor,
            "far_tenor": far_tenor,
            "commodity_name": name,
            "spread_type": "time",
            "label": f"{name} {near_tenor}-{far_tenor}",
        }
    )
    return out

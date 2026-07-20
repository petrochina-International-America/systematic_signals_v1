import pandas as pd


def construct_cross_arb(
    prices1: pd.DataFrame,
    prices2: pd.DataFrame,
    tenor1: str = "F1",
    tenor2: str = "F2",
    *,
    leg1_name: str | None = None,
    leg2_name: str | None = None,
    leg1_weight: float = 1.0,
    leg2_weight: float = 1.0,
) -> pd.DataFrame:
    """
    Construct a cross-arb spread: different commodity AND different tenor.
        spread = leg1_weight * prices1[tenor1] - leg2_weight * prices2[tenor2]

    This generalises both time and product spreads:
    - Same commodity, different tenors  → time spread (use construct_time_spread instead)
    - Same tenor, different commodities → product spread (use construct_product_spread instead)
    - Different commodity + different tenor → cross-arb (this function)

    Prices for both legs must already be normalized to a compatible unit
    (e.g. both in $/BBL via load_prices with normalize=True) before calling.

    Parameters
    ----------
    prices1 : DataFrame
        DatetimeIndex, columns F1, F2, ... for commodity 1.
    prices2 : DataFrame
        DatetimeIndex, columns F1, F2, ... for commodity 2.
    tenor1 : str
        Contract month for leg 1, e.g. "F1".
    tenor2 : str
        Contract month for leg 2, e.g. "F3".
    leg1_name : str, optional
        Label for leg 1; falls back to prices1.attrs["commodity_name"].
    leg2_name : str, optional
        Label for leg 2; falls back to prices2.attrs["commodity_name"].
    leg1_weight : float
        Scalar multiplier for leg 1 (default 1.0).
    leg2_weight : float
        Scalar multiplier for leg 2 (default 1.0).

    Returns
    -------
    DataFrame
        Columns: leg1_price, leg2_price, spread.
        Index: DatetimeIndex (intersection of both price series, non-NaN).
        Attributes: leg1_name, leg2_name, tenor1, tenor2, leg1_weight,
                    leg2_weight, spread_type, label.
    """
    if tenor1 not in prices1.columns:
        raise ValueError(
            f"tenor1 '{tenor1}' not in prices1 columns: {list(prices1.columns)}"
        )
    if tenor2 not in prices2.columns:
        raise ValueError(
            f"tenor2 '{tenor2}' not in prices2 columns: {list(prices2.columns)}"
        )

    leg1 = prices1[tenor1].rename("leg1_price")
    leg2 = prices2[tenor2].rename("leg2_price")

    out = pd.concat([leg1, leg2], axis=1).dropna()
    out["spread"] = leg1_weight * out["leg1_price"] - leg2_weight * out["leg2_price"]

    name1 = leg1_name or prices1.attrs.get("commodity_name", "leg1")
    name2 = leg2_name or prices2.attrs.get("commodity_name", "leg2")
    out.attrs.update(
        {
            "leg1_name": name1,
            "leg2_name": name2,
            "tenor1": tenor1,
            "tenor2": tenor2,
            "leg1_weight": leg1_weight,
            "leg2_weight": leg2_weight,
            "spread_type": "cross_arb",
            "label": f"{name1} {tenor1} / {name2} {tenor2}",
        }
    )
    return out

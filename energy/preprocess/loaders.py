import re
from collections.abc import Iterable

import pandas as pd

from energy.accounting.contract_specs import CONTRACT_SPECS
from energy.preprocess.read_data import read_data
from energy.preprocess.drop_dupes import drop_dupes
from energy.preprocess.expiry_calendar import expiry_calendar

_FCOL_PATTERN = re.compile(r"^F\d+$")


def get_contract_spec(commodity_name: str) -> dict:
    if commodity_name not in CONTRACT_SPECS:
        raise KeyError(f"Commodity '{commodity_name}' not found in CONTRACT_SPECS.")
    return CONTRACT_SPECS[commodity_name]


def _normalize_commodity_input(commodities: str | Iterable[str]) -> list[str]:
    """
    Accept either:
        "Butane"
        ["Butane", "Propane"]
        {"Butane", "Propane"}

    and always return a validated list[str].
    """
    if isinstance(commodities, str):
        names = [commodities]
    elif isinstance(commodities, Iterable):
        names = list(commodities)
    else:
        raise TypeError(
            "commodities must be a string or an iterable of strings."
        )

    if not names:
        raise ValueError("No commodities were provided.")

    bad_types = [x for x in names if not isinstance(x, str)]
    if bad_types:
        raise TypeError(
            f"All commodity names must be strings. Invalid entries: {bad_types}"
        )

    missing = [name for name in names if name not in CONTRACT_SPECS]
    if missing:
        raise KeyError(
            f"Unknown commodities: {missing}. "
            f"These must match keys in CONTRACT_SPECS."
        )

    return names


def load_prices(
    commodity_name: str,
    data_path,
    *,
    normalize: bool = True,
    drop_duplicates: bool = True,
    sort_index: bool = True,
) -> pd.DataFrame:
    spec = get_contract_spec(commodity_name)
    ticker = spec["ticker"]
    sheet_name = f"{commodity_name} ({ticker})"

    df = read_data(data_path, sheet=sheet_name)

    if drop_duplicates:
        df = drop_dupes(df)

    if sort_index:
        df = df.sort_index()

    if normalize:
        scale = spec.get("normalization", 1.0)
        fcols = [c for c in df.columns if _FCOL_PATTERN.match(str(c))]
        if scale is not None and scale != 1.0 and fcols:
            df.loc[:, fcols] = df[fcols] * scale
        df.attrs["norm_scale"] = scale
    else:
        df.attrs["norm_scale"] = 1.0

    df.attrs["commodity_name"] = commodity_name
    df.attrs["ticker"] = ticker
    return df


def load_expiry(commodity_name: str, calendar_path) -> pd.DatetimeIndex:
    spec = get_contract_spec(commodity_name)
    ticker = spec["ticker"]
    return expiry_calendar(ticker, calendar_path)


def align_to_intersection(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """
    Reindex a dict of {name: DataFrame} onto their shared intersection
    calendar (only dates present in EVERY frame), sorted ascending.

    Generic alignment helper — mirrors the `intersection_idx` step from
    export_to_excel.py. Works on any DatetimeIndex-keyed frames regardless
    of where they were sourced (Excel, FlowsDB, etc.).
    """
    if not frames:
        return {}

    common_idx: pd.DatetimeIndex | None = None
    for df in frames.values():
        common_idx = df.index if common_idx is None else common_idx.intersection(df.index)
    common_idx = common_idx.sort_values()

    return {name: df.reindex(common_idx) for name, df in frames.items()}


def load_contract_data(
    commodities: str | Iterable[str],
    data_path,
    calendar_path,
    *,
    normalize: bool = True,
    align: bool = True,
):
    """
    Backward-compatible loader.

    If a single commodity is passed:
        returns (prices, expiry)

    If multiple commodities are passed:
        returns {
            "Butane": {
                "prices": <DataFrame>,
                "expiry": <DatetimeIndex>,
            },
            ...
        }
    By default (`align=True`), each commodity's `prices` frame is reindexed
    onto the shared intersection calendar across all requested commodities
    (see `align_to_intersection`), so multi-commodity series line up
    symmetrically. Pass `align=False` to keep each commodity's native index.
    """
    names = _normalize_commodity_input(commodities)

    if len(names) == 1:
        commodity_name = names[0]
        prices = load_prices(
            commodity_name,
            data_path=data_path,
            normalize=normalize,
        )
        expiry = load_expiry(
            commodity_name,
            calendar_path=calendar_path,
        )
        return prices, expiry

    out = {}
    for commodity_name in names:
        prices = load_prices(
            commodity_name,
            data_path=data_path,
            normalize=normalize,
        )
        expiry = load_expiry(
            commodity_name,
            calendar_path=calendar_path,
        )

        out[commodity_name] = {
            "prices": prices,
            "expiry": expiry,
        }

    if align:
        aligned_prices = align_to_intersection({name: v["prices"] for name, v in out.items()})
        for name in out:
            out[name]["prices"] = aligned_prices[name]

    return out
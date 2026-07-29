"""
data/readback.py — reconstruct the API payloads from the systematic.* schema.

The mirror image of data.publish. Every function here returns the SAME JSON
shape as the corresponding route in api/, built purely from published rows —
no price store, no strategy engines, no warm-up. That is what lets the shared
work dashboard serve the Signals and Levels tabs from FlowsDB alone.

It also doubles as the round-trip test for the publisher: if readback output
matches the live endpoint output for the same date, the schema is lossless for
what those two tabs render.

Everything defaults to the latest successfully published date
(systematic.v_latest_date), so callers never handle the pre-open gap.
"""

import json
import math
import os
from typing import Any

import pandas as pd

# Which schema to read. The us_analysts landing schema mirrors systematic.*
# name-for-name (see data/us_analysts_schema.sql), so pointing this at
# 'us_analysts' makes every function here read the replicated copy instead.
_SCHEMA = os.getenv("READBACK_SCHEMA", "systematic")


def set_schema(schema: str) -> None:
    """Override the source schema ('systematic' | 'us_analysts')."""
    global _SCHEMA
    if schema not in ("systematic", "us_analysts"):
        raise ValueError(f"unknown readback schema: {schema!r}")
    _SCHEMA = schema


# ── helpers ───────────────────────────────────────────────────────────────────


def _clean(v: Any) -> Any:
    """pandas NA / NaN / numpy scalars -> JSON-safe Python."""
    if v is None or v is pd.NaT:
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if pd.api.types.is_scalar(v) and pd.isna(v):
        return None
    if hasattr(v, "item"):          # numpy scalar
        v = v.item()
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def _records(df: pd.DataFrame) -> list[dict]:
    return [{k: _clean(v) for k, v in row.items()} for row in df.to_dict("records")]


def _payload(raw: Any) -> Any:
    """jsonb columns arrive as dict (psycopg2) or str depending on driver."""
    return json.loads(raw) if isinstance(raw, str) else raw


def _whole(v: Any) -> Any:
    """NUMERIC round-trips 23 as 23.0. Restore the int where the producer
    rounded to one, so readback output compares equal to the live payload."""
    v = _clean(v)
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def latest_date() -> str | None:
    """Most recent as_of_date with a successful publish_run."""
    from data.db import query_df
    df = query_df(f"SELECT as_of_date FROM {_SCHEMA}.v_latest_date")
    if df.empty or df["as_of_date"].iloc[0] is None:
        return None
    return str(df["as_of_date"].iloc[0])


def _resolve(as_of: str | None) -> str | None:
    return as_of or latest_date()


# ── /api/signals/snapshot ─────────────────────────────────────────────────────


def signal_snapshot(as_of: str | None = None) -> dict:
    from data.db import query_df
    from data.signals import PRODUCT_GROUPS, STRATEGIES

    as_of = _resolve(as_of)
    df = query_df(
        f"SELECT commodity, strategy, detail FROM {_SCHEMA}.signal_outright "
        "WHERE as_of_date = :d", {"d": as_of},
    )

    # `detail` is the cell as the engine produced it — serving it verbatim keeps
    # Momentum's ma_value/pct_from_ma and Carry's spread/spread_pct/end_tenor
    # without this layer needing to know which strategy has which fields.
    signals: dict[str, dict] = {}
    for r in _records(df):
        signals.setdefault(r["commodity"], {})[r["strategy"]] = _payload(r["detail"])
    # Commodities that never published (no FlowsDB ticker yet) still need a cell
    for commodities in PRODUCT_GROUPS.values():
        for c in commodities:
            cell = signals.setdefault(c, {})
            for s in STRATEGIES:
                cell.setdefault(s, {"direction": "—", "conviction": None})

    return {
        "product_groups": PRODUCT_GROUPS,
        "strategies": STRATEGIES,
        "signals": signals,
        "as_of_date": as_of,
    }


# ── /api/signals/spreads ──────────────────────────────────────────────────────

_SPREAD_FIELDS = [
    "pair", "direction", "zscore", "spread_value", "quoted_spread",
    "spread_mean", "deviation", "pct_from_mean", "dist_to_threshold",
    "pct_from_threshold", "lookback", "threshold", "month_offset",
    "construction", "precision_mode", "signal_series",
]


def spread_snapshot(as_of: str | None = None) -> dict:
    from data.db import query_df
    from energy.analytics.signal_summary import SPREAD_GROUPS

    as_of = _resolve(as_of)
    df = query_df(
        f"SELECT * FROM {_SCHEMA}.signal_spread WHERE as_of_date = :d",
        {"d": as_of},
    )
    by_pair = {r["pair"]: r for r in _records(df)}

    result: dict[str, list] = {}
    for group, pairs in SPREAD_GROUPS.items():
        entries = []
        for leg1, leg2 in pairs:
            pair = f"{leg1} / {leg2}"
            row = by_pair.get(pair)
            if row is None:
                entries.append({"pair": pair, "direction": "—", "zscore": None,
                                "pct_from_mean": None, "lookback": None,
                                "threshold": None})
                continue
            entries.append({k: row.get(k) for k in _SPREAD_FIELDS})
        result[group] = entries
    return result


# ── /api/signals/top-performers ───────────────────────────────────────────────


def top_performers(n: int = 5, as_of: str | None = None) -> dict:
    from data.db import query_df

    as_of = _resolve(as_of)

    def _bar(rank_col: str, sharpe_col: str) -> list[dict]:
        df = query_df(
            f"SELECT label, strategy, instrument, direction, {sharpe_col} "
            f"FROM {_SCHEMA}.strategy_performance "
            f"WHERE as_of_date = :d AND {rank_col} IS NOT NULL "
            f"ORDER BY {rank_col} LIMIT :n",
            {"d": as_of, "n": n},
        )
        # Stored full-precision; the live bar rounds for display before
        # serving, so round here too — same JSON contract, either source.
        return [{"commodity": r["instrument"], "strategy": r["strategy"],
                 "label": r["label"], "direction": r["direction"],
                 sharpe_col: (round(r[sharpe_col], 2)
                              if r[sharpe_col] is not None else None)}
                for r in _records(df)]

    return {
        "top_1y": _bar("rank_1y", "sharpe_1y"),
        "top_alltime": _bar("rank_all", "sharpe_all"),
    }


# ── /api/levels/proximity ─────────────────────────────────────────────────────


def proximity(as_of: str | None = None) -> dict:
    from data.db import query_df
    from data.signals import PRODUCT_GROUPS

    as_of = _resolve(as_of)

    cards_df = query_df(
        f"SELECT * FROM {_SCHEMA}.levels_card WHERE as_of_date = :d "
        "ORDER BY closest_dist NULLS LAST",
        {"d": as_of},
    )
    series_df = query_df(
        f"SELECT scope, series_key, payload FROM {_SCHEMA}.chart_series "
        "WHERE as_of_date = :d", {"d": as_of},
    )
    hot_df = query_df(
        f"SELECT * FROM {_SCHEMA}.levels_hot WHERE as_of_date = :d "
        "ORDER BY distance NULLS LAST", {"d": as_of},
    )
    flip_df = query_df(
        f"SELECT * FROM {_SCHEMA}.levels_flip WHERE as_of_date = :d "
        "ORDER BY flip_date DESC", {"d": as_of},
    )

    card_series, spread_series = {}, {}
    for r in _records(series_df):
        target = card_series if r["scope"] == "levels_card" else spread_series
        target[r["series_key"]] = _payload(r["payload"])

    groups: dict[str, list] = {g: [] for g in PRODUCT_GROUPS}
    for r in _records(cards_df):
        s = card_series.get(r["commodity"], {})
        card = {
            "commodity": r["commodity"],
            "tenor_label": s.get("tenor_label"),
            "tenor_col": s.get("tenor_col"),
            "dates": s.get("dates", []),
            "prices": s.get("prices", []),
            "current": r["current_price"],
            "ma_levels": s.get("ma_levels", []),
            "carry": {
                "direction": r["carry_direction"],
                "distance_pct": r["carry_distance_pct"],
                "level": ({
                    "tenor": r["carry_tenor"],
                    "value": r["carry_level"],
                    "shape": r["carry_shape"],
                    "spread": r["carry_spread"],
                    "history": s.get("carry_history"),
                } if r["carry_tenor"] else None),
            },
            "cot": ({"percentile": _whole(r["cot_percentile"]), "flag": r["cot_flag"],
                     "pending": r["cot_pending"]}
                    if r["cot_percentile"] is not None else None),
            "cta": {
                "direction": r["cta_direction"],
                "net_signal": r["cta_net_signal"],
                "weights": s.get("weights"),
                "position_pct": r["position_pct"],
                "position_pct_prev": r["position_pct_prev"],
                "position_chg": r["position_chg"],
                "vol_scalar": r["vol_scalar"],
                "position_history": s.get("position_history", []),
                "position_util_history": s.get("position_util_history", []),
            },
            "closest_dist": r["closest_dist"],
        }
        groups.setdefault(r["product_group"] or "Other", []).append(card)

    hot = [{"commodity": r["instrument"], "strategy": r["strategy"],
            "direction": r["direction"], "distance": r["distance"],
            "detail": r["detail"], "level": r["level"], "current": r["current"]}
           for r in _records(hot_df)]

    recent_trades = [{"commodity": r["instrument"], "strategy": r["strategy"],
                      "from": r["from_direction"], "to": r["to_direction"],
                      "price": r["price"], "level": r["level"],
                      "date": str(r["flip_date"]), "tier": r["tier"]}
                     for r in _records(flip_df)]

    return {
        "groups": groups,
        "spreads": spread_series,
        "hot": hot,
        "recent_trades": recent_trades,
        "as_of_date": as_of,
    }


# ── /api/sizing/today ─────────────────────────────────────────────────────────


def sizing(strategy: str | None = None, instrument: str | None = None,
           as_of: str | None = None) -> list[dict]:
    """Published sizing rows, optionally filtered. Returns the full stored
    payload per run so pair leg breakdowns survive."""
    from data.db import query_df

    as_of = _resolve(as_of)
    clauses, params = ["as_of_date = :d"], {"d": as_of}
    if strategy:
        clauses.append("strategy = :s")
        params["s"] = strategy
    if instrument:
        clauses.append("instrument = :i")
        params["i"] = instrument

    df = query_df(
        f"SELECT strategy, instrument, is_pair, payload FROM {_SCHEMA}.sizing_daily "
        f"WHERE {' AND '.join(clauses)} ORDER BY strategy, instrument", params,
    )
    return [{"strategy": r["strategy"], "instrument": r["instrument"],
             "is_pair": r["is_pair"], **_payload(r["payload"])}
            for r in _records(df)]

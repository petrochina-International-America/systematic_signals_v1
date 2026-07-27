"""
data/publish.py — publish computed outputs into the systematic.* schema.

The dashboard today computes everything in-process and serves it over HTTP;
nothing is persisted (see ARCHITECTURE.md "Single-process cache"). This module
is the write path: it runs the SAME functions the API routes run, flattens
their payloads into rows, and upserts them into FlowsDB under `systematic`.
From there the shared work dashboard reads — FlowsDB stays the single source
of truth and there is exactly one publish hop.

Deliberately reuses the API's own producers (api.signals.*, api.levels.*,
api.sizing.*) rather than re-deriving anything. If a number here disagrees with
the dashboard, that is a bug in this file, not a second opinion.

Scope: what the Signals and Levels tabs need. The unbounded lab parameter space
is NOT published — only `canonical_runs()` below, which is the explicit answer
to "which runs are worth storing".

Usage:
    py -3.14 -m data.publish --init           # create the schema, then publish
    py -3.14 -m data.publish                  # publish the latest trading date
    py -3.14 -m data.publish --dry-run        # compute and report, write nothing
    py -3.14 -m data.publish --require-fresh  # fail if prices have not advanced

Scheduled runs should use --require-fresh, or trigger POST /api/admin/publish
on the running API (which re-warms the price store first). See the pipeline
notes in ARCHITECTURE.md.
"""

import json
import math
import os
import sys
import traceback
from typing import Any

# ── numeric hygiene ───────────────────────────────────────────────────────────


def _num(v: Any) -> float | None:
    """NaN/Inf/None -> None; everything else -> float. Postgres NUMERIC accepts
    'NaN', so without this the DB would happily store unusable values."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _int(v: Any) -> int | None:
    f = _num(v)
    return int(f) if f is not None else None


def _str(v: Any) -> str | None:
    return None if v is None else str(v)


# ── group lookups ─────────────────────────────────────────────────────────────


def _product_group_of() -> dict[str, str]:
    from data.signals import PRODUCT_GROUPS
    return {c: g for g, cs in PRODUCT_GROUPS.items() for c in cs}


def _spread_group_of() -> dict[str, str]:
    from energy.analytics.signal_summary import SPREAD_GROUPS
    return {f"{l1} / {l2}": g for g, ps in SPREAD_GROUPS.items() for l1, l2 in ps}


def _slash_pair(label: str) -> str:
    """Levels labels pairs with U+2212 ('WTI − Brent'); Signals and the lab use
    'WTI / Brent'. Normalize to the slash form so both sides key alike."""
    return label.replace(" − ", " / ")


# ── canonical run universe ────────────────────────────────────────────────────
#
# Mirrors api.signals._compute_top_performers exactly: Carry once per commodity,
# Momentum once per speed tier, Stat-Arb once per pair, each on pair_defaults().
# Widen this list to publish more of the lab; everything not listed stays
# on-demand compute.

_MOMENTUM_TIERS = ["Very Fast", "Fast", "Medium", "Slow", "Averaged"]


def canonical_runs() -> list[dict]:
    """The pinned set of lab runs that get persisted, as param dicts."""
    from data.signals import PRODUCT_GROUPS
    from energy.analytics.signal_summary import SPREAD_GROUPS

    runs: list[dict] = []
    for commodities in PRODUCT_GROUPS.values():
        for commodity in commodities:
            runs.append({
                "params": {"strategy": "Carry", "commodity": commodity},
                "strategy": "Carry", "instrument": commodity,
                "label": f"{commodity} Carry",
            })
            for tier in _MOMENTUM_TIERS:
                runs.append({
                    "params": {"strategy": "Momentum", "commodity": commodity,
                               "tier": tier},
                    "strategy": "Momentum", "instrument": commodity,
                    "label": f"{commodity} Mom ({tier})",
                })
    for pairs in SPREAD_GROUPS.values():
        for leg1, leg2 in pairs:
            pair = f"{leg1} / {leg2}"
            runs.append({
                "params": {"strategy": "Stat-Arb", "pair": pair},
                "strategy": "Stat-Arb", "instrument": pair,
                "label": f"{pair} Mean Rev",
            })
    return runs


# ── section publishers ────────────────────────────────────────────────────────
#
# Each returns (table_name, rows, conflict_cols, jsonb_cols) tuples via _write.


def _write(table, rows, conflict_cols, jsonb_cols=(), touch_col="updated_at",
           dry_run=False) -> int:
    if dry_run or not rows:
        return len(rows)
    from data.db import upsert
    return upsert(table, rows, conflict_cols, jsonb_cols, touch_col)


def publish_signals(as_of: str, dry_run: bool = False) -> dict[str, int]:
    """signal_outright + signal_spread, from /api/signals/{snapshot,spreads}
    merged with the spread panel of /api/levels/proximity."""
    from api.signals import signal_snapshot, spread_snapshot
    from api.levels import proximity

    pgroup = _product_group_of()
    sgroup = _spread_group_of()

    # ── outrights ──
    snap = signal_snapshot()
    outright_rows = []
    for commodity, per_strategy in snap["signals"].items():
        for strategy, cell in per_strategy.items():
            # Momentum and Carry cells carry different strength fields, and the
            # no-ticker fallback carries neither — keep the cell verbatim in
            # `detail` and break out whatever is present into columns.
            outright_rows.append({
                "as_of_date": as_of,
                "commodity": commodity,
                "strategy": strategy,
                "product_group": pgroup.get(commodity),
                "direction": _str(cell.get("direction")),
                "ma_value": _num(cell.get("ma_value")),
                "pct_from_ma": _num(cell.get("pct_from_ma")),
                "spread": _num(cell.get("spread")),
                "spread_pct": _num(cell.get("spread_pct")),
                "end_tenor": _str(cell.get("end_tenor")),
                "detail": json.dumps(cell, default=str),
            })

    # ── spreads: Signals-tab fields ──
    # Explicit None: these are FastAPI Query() defaults, so calling the route
    # function bare would pass Query objects straight into the engine.
    spreads = spread_snapshot(lookback=None, threshold=None)
    spread_rows: dict[str, dict] = {}
    for group, entries in spreads.items():
        for e in entries:
            pair = e["pair"]
            leg1, _, leg2 = pair.partition(" / ")
            spread_rows[pair] = {
                "as_of_date": as_of,
                "pair": pair,
                "spread_group": sgroup.get(pair, group),
                "leg1": leg1 or None,
                "leg2": leg2 or None,
                "direction": _str(e.get("direction")),
                "zscore": _num(e.get("zscore")),
                "spread_value": _num(e.get("spread_value")),
                "quoted_spread": _num(e.get("quoted_spread")),
                "spread_mean": _num(e.get("spread_mean")),
                "spread_std": None,
                "upper_band": None,
                "lower_band": None,
                "deviation": _num(e.get("deviation")),
                "pct_from_mean": _num(e.get("pct_from_mean")),
                "dist_to_threshold": _num(e.get("dist_to_threshold")),
                "pct_from_threshold": _num(e.get("pct_from_threshold")),
                "in_trade": None,
                "signal_prev": None,
                "lookback": _int(e.get("lookback")),
                "threshold": _num(e.get("threshold")),
                "month_offset": _int(e.get("month_offset")),
                "construction": _str(e.get("construction")),
                "precision_mode": _str(e.get("precision_mode")),
                "signal_series": _str(e.get("signal_series")),
            }

    # ── spreads: band/std/in-trade columns only the Levels panel computes ──
    for label, s in proximity()["spreads"].items():
        pair = _slash_pair(label)
        row = spread_rows.get(pair)
        if row is None:
            continue
        row["spread_std"] = _num(s.get("spread_std"))
        row["upper_band"] = _num(s.get("upper"))
        row["lower_band"] = _num(s.get("lower"))
        row["in_trade"] = bool(s.get("in_trade")) if s.get("in_trade") is not None else None
        row["signal_prev"] = _num(s.get("signal_prev"))

    return {
        "signal_outright": _write("systematic.signal_outright", outright_rows,
                                  ["as_of_date", "commodity", "strategy"],
                                  jsonb_cols=("detail",), dry_run=dry_run),
        "signal_spread": _write("systematic.signal_spread", list(spread_rows.values()),
                                ["as_of_date", "pair"], dry_run=dry_run),
    }


def publish_performance(as_of: str, dry_run: bool = False) -> dict[str, int]:
    """strategy_run + strategy_performance — the top-performers bar.

    Runs the canonical set through data.lab and scores it with the API's own
    _sharpe_window, so the stored Sharpe is bit-identical to the rendered one.
    Ranks come from _best_momentum_per_commodity (only a commodity's best
    momentum tier ranks); non-ranking runs are stored with NULL rank.
    """
    from data import lab
    from api.signals import _sharpe_window, _best_momentum_per_commodity

    run_rows, entries = [], []
    for spec in canonical_runs():
        try:
            key = lab.run_lab(spec["params"])
            result = lab.get_result(key)
            pos = float(result["position"].iloc[-1])
        except Exception:
            continue  # same tolerance as _compute_top_performers

        direction = "Long" if pos > 0 else ("Short" if pos < 0 else "Flat")
        run_rows.append({
            "run_key": key,
            "strategy": spec["strategy"],
            "instrument": spec["instrument"],
            "label": spec["label"],
            "params": json.dumps(json.loads(key), sort_keys=True),
            "last_computed": "now()",
        })
        entries.append({
            "run_key": key,
            "label": spec["label"],
            "strategy": spec["strategy"],
            "commodity": spec["instrument"],   # key name _best_momentum_* expects
            "direction": direction,
            "sharpe_1y": _sharpe_window(key, 252),
            "sharpe_all": _sharpe_window(key, None),
        })

    # last_computed is a real timestamp column — send a value, not SQL text
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc)
    for r in run_rows:
        r["last_computed"] = stamp

    def _ranked(field: str) -> dict[str, int]:
        scored = [e for e in entries if e[field] is not None]
        scored.sort(key=lambda e: e[field], reverse=True)
        kept = _best_momentum_per_commodity(scored)
        return {e["run_key"]: i + 1 for i, e in enumerate(kept)}

    rank_1y, rank_all = _ranked("sharpe_1y"), _ranked("sharpe_all")

    perf_rows = [{
        "as_of_date": as_of,
        "run_key": e["run_key"],
        "label": e["label"],
        "strategy": e["strategy"],
        "instrument": e["commodity"],
        "direction": e["direction"],
        "sharpe_1y": _num(e["sharpe_1y"]),
        "sharpe_all": _num(e["sharpe_all"]),
        "rank_1y": rank_1y.get(e["run_key"]),
        "rank_all": rank_all.get(e["run_key"]),
    } for e in entries]

    n_runs = _write("systematic.strategy_run", run_rows, ["run_key"],
                    jsonb_cols=("params",), touch_col=None, dry_run=dry_run)
    n_perf = _write("systematic.strategy_performance", perf_rows,
                    ["as_of_date", "run_key"], dry_run=dry_run)
    return {"strategy_run": n_runs, "strategy_performance": n_perf}


def publish_levels(as_of: str, dry_run: bool = False) -> dict[str, int]:
    """levels_card + levels_hot + levels_flip + chart_series, from
    /api/levels/proximity. Scalars go to columns; the 63-day display arrays go
    to chart_series as JSONB."""
    from api.levels import proximity

    prox = proximity()
    pgroup = _product_group_of()

    card_rows, series_rows = [], []
    for group_name, cards in prox["groups"].items():
        for c in cards:
            carry = c.get("carry") or {}
            level = carry.get("level") or {}
            cta = c.get("cta") or {}
            cot = c.get("cot") or {}
            commodity = c["commodity"]

            card_rows.append({
                "as_of_date": as_of,
                "commodity": commodity,
                "product_group": pgroup.get(commodity, group_name),
                "current_price": _num(c.get("current")),
                "mom_direction": _str(_mom_direction(c)),
                "mom_distance_pct": _num(_mom_distance(c)),
                "carry_direction": _str(carry.get("direction")),
                "carry_distance_pct": _num(carry.get("distance_pct")),
                "carry_tenor": _str(level.get("tenor")),
                "carry_level": _num(level.get("value")),
                "carry_shape": _str(level.get("shape")),
                "carry_spread": _num(level.get("spread")),
                "cta_direction": _str(cta.get("direction")),
                "cta_net_signal": _num(cta.get("net_signal")),
                "position_pct": _num(cta.get("position_pct")),
                "position_pct_prev": _num(cta.get("position_pct_prev")),
                "position_chg": _num(cta.get("position_chg")),
                "vol_scalar": _num(cta.get("vol_scalar")),
                "cot_percentile": _num(cot.get("percentile")),
                "cot_flag": _str(cot.get("flag")),
                "cot_pending": bool(cot.get("pending")) if cot else None,
                "closest_dist": _num(c.get("closest_dist")),
            })

            series_rows.append({
                "as_of_date": as_of,
                "scope": "levels_card",
                "series_key": commodity,
                "payload": json.dumps({
                    "dates": c.get("dates", []),
                    "prices": c.get("prices", []),
                    "ma_levels": c.get("ma_levels", []),
                    "carry_history": level.get("history"),
                    "position_history": cta.get("position_history", []),
                    "position_util_history": cta.get("position_util_history", []),
                }),
            })

    # Spread panel: entirely presentation-shaped (bands + histories), so it
    # rides in chart_series. The queryable scalars are already in signal_spread.
    for label, s in prox["spreads"].items():
        series_rows.append({
            "as_of_date": as_of,
            "scope": "levels_spread",
            "series_key": label,
            "payload": json.dumps(s),
        })

    hot_rows = [{
        "as_of_date": as_of,
        "instrument": h["commodity"],
        "strategy": h["strategy"],
        "direction": _str(h.get("direction")),
        "distance": _num(h.get("distance")),
        "detail": _str(h.get("detail")),
        "level": _num(h.get("level")),
        "current": _num(h.get("current")),
    } for h in prox["hot"]]

    flip_rows = [{
        "as_of_date": as_of,
        "instrument": t["commodity"],
        "flip_date": t["date"],
        "strategy": t["strategy"],
        "tier": _str(t.get("tier")),
        "from_direction": _str(t.get("from")),
        "to_direction": _str(t.get("to")),
        "price": _num(t.get("price")),
        "level": _num(t.get("level")),
    } for t in prox["recent_trades"] if t.get("date")]

    return {
        "levels_card": _write("systematic.levels_card", card_rows,
                              ["as_of_date", "commodity"], dry_run=dry_run),
        "levels_hot": _write("systematic.levels_hot", hot_rows,
                             ["as_of_date", "instrument", "strategy"], dry_run=dry_run),
        "levels_flip": _write("systematic.levels_flip", flip_rows,
                              ["as_of_date", "instrument", "flip_date", "strategy"],
                              dry_run=dry_run),
        "chart_series": _write("systematic.chart_series", series_rows,
                               ["as_of_date", "scope", "series_key"],
                               jsonb_cols=("payload",), dry_run=dry_run),
    }


def _mom_direction(card: dict) -> str | None:
    """Levels cards carry momentum direction implicitly (price vs the Medium MA);
    proximity() computes it but only surfaces it through closest_dist."""
    mas = card.get("ma_levels") or []
    if not mas:
        return None
    med = next((m for m in mas if m.get("tier") == "Medium"), mas[0])
    current = card.get("current")
    if current is None or med.get("value") is None:
        return None
    return "Long" if current > med["value"] else "Short"


def _mom_distance(card: dict) -> float | None:
    """% distance to the closest meaningful (>=20d) MA — the near-trigger metric."""
    mas = card.get("ma_levels") or []
    current = card.get("current")
    if not mas or current is None:
        return None
    meaningful = [m for m in mas if (m.get("window") or 0) >= 20] or mas
    closest = min(meaningful, key=lambda m: abs(current - m["value"]))
    if not closest.get("value"):
        return None
    return round((current / closest["value"] - 1) * 100, 2)


def publish_sizing(as_of: str, dry_run: bool = False) -> dict[str, int]:
    """sizing_daily over the calibrated universe in SIZING_CONFIGS.

    Uses the SIZING_CONFIGS ref_price defaults — a batch job has no live price
    override — so these are calibration-basis lots, not desk-live lots.
    """
    from api.sizing import SizingRequest, todays_size
    from data import lab
    from energy.sizing.daily_size import SIZING_CONFIGS

    rows = []
    for strategy, instruments in SIZING_CONFIGS.items():
        is_pair = strategy in ("Stat-Arb", "RV")
        for instrument in instruments:
            params = ({"strategy": strategy, "pair": instrument} if is_pair
                      else {"strategy": strategy, "commodity": instrument})
            try:
                payload = todays_size(SizingRequest(**params))
                run_key = lab.run_lab(params)
            except Exception:
                continue

            legs = payload.get("legs") or {}
            rows.append({
                "as_of_date": as_of,
                "run_key": run_key,
                "strategy": strategy,
                "instrument": instrument,
                "is_pair": is_pair,
                "signal": _num(payload.get("signal")),
                "direction": _str(payload.get("direction")),
                "vol_scalar": _num(payload.get("scalar")),
                "realized_vol_ann_pct": _num(payload.get("realized_vol_ann_pct")),
                "lots": None if is_pair else _num(payload.get("lots")),
                "notional_usd": _num(payload.get("total_notional_usd") if is_pair
                                     else payload.get("notional_usd")),
                "var_95_usd": _num(payload.get("total_var_95_usd") if is_pair
                                   else payload.get("var_95_usd")),
                "capital_base": _num(payload.get("capital_base")),
                "ref_price": None if is_pair else _num(payload.get("ref_price")),
                "sizing_mode": _str(payload.get("sizing_mode")),
                "payload": json.dumps(payload, default=str),
            })

    return {"sizing_daily": _write("systematic.sizing_daily", rows,
                                   ["as_of_date", "run_key"],
                                   jsonb_cols=("payload",), dry_run=dry_run)}


# ── orchestration ─────────────────────────────────────────────────────────────


def init_schema() -> None:
    """Apply data/schema.sql (idempotent — everything is IF NOT EXISTS)."""
    from data.db import execute_script
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    with open(path, encoding="utf-8") as fh:
        execute_script(fh.read())


def last_published_date() -> str | None:
    """Latest as_of_date with a successful publish_run, or None."""
    from data.db import query_df
    df = query_df("SELECT as_of_date FROM systematic.v_latest_date")
    if df.empty or df["as_of_date"].iloc[0] is None:
        return None
    return str(df["as_of_date"].iloc[0])


def publish(as_of: str | None = None, dry_run: bool = False,
            require_fresh: bool = False) -> dict:
    """
    Warm the data store, compute every published surface, write it, and record
    the attempt in systematic.publish_run.

    as_of defaults to the latest trading date in the price store — never
    wall-clock, so a run before the daily FlowsDB pull republishes yesterday
    rather than writing an empty row under today's date.

    require_fresh=True refuses to run when the price store has not advanced past
    the last successful publish. Scheduled runs should set it: without it, firing
    before the Bloomberg pull commits silently republishes yesterday's signals
    and still records status='ok', so the shared dashboard would show today's
    prices next to yesterday's signals with nothing flagging the mismatch.
    Manual re-runs leave it off so a same-day republish still works.
    """
    from data import loader
    from data.db import execute

    if not loader.loaded_commodities():
        loader.warm_up()
    as_of = as_of or loader.latest_data_date()
    if not as_of:
        raise RuntimeError("No price data loaded — cannot determine as_of_date.")

    if require_fresh:
        previous = last_published_date()
        if previous is not None and as_of <= previous:
            raise RuntimeError(
                f"Price store has not advanced: latest trading date is {as_of}, "
                f"already published through {previous}. The upstream Bloomberg "
                f"-> prices_daily load has probably not committed yet."
            )

    run_id = None
    if not dry_run:
        run_id = execute(
            "INSERT INTO systematic.publish_run (as_of_date) VALUES (:d) "
            "RETURNING run_id", {"d": as_of},
        ).scalar_one()

    counts: dict[str, int] = {}
    try:
        counts.update(publish_signals(as_of, dry_run))
        counts.update(publish_performance(as_of, dry_run))
        counts.update(publish_levels(as_of, dry_run))
        counts.update(publish_sizing(as_of, dry_run))
    except Exception:
        if run_id is not None:
            execute(
                "UPDATE systematic.publish_run SET status='failed', "
                "finished_at=now(), error=:e WHERE run_id=:i",
                {"e": traceback.format_exc()[:8000], "i": run_id},
            )
        raise

    total = sum(counts.values())
    if run_id is not None:
        execute(
            "UPDATE systematic.publish_run SET status='ok', finished_at=now(), "
            "rows_written=:n WHERE run_id=:i", {"n": total, "i": run_id},
        )

    return {"as_of_date": as_of, "run_id": run_id, "rows": total, "tables": counts}


if __name__ == "__main__":
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(_here))                    # systematic-trading/
    sys.path.insert(0, os.path.dirname(os.path.dirname(_here)))   # repo root (energy/)

    dry = "--dry-run" in sys.argv
    if "--init" in sys.argv:
        init_schema()
        print("schema applied: systematic")

    summary = publish(dry_run=dry, require_fresh="--require-fresh" in sys.argv)
    print(("DRY RUN " if dry else "") + f"as_of={summary['as_of_date']} "
          f"rows={summary['rows']} run_id={summary['run_id']}")
    for table, n in sorted(summary["tables"].items()):
        print(f"  {table:<24} {n:>5}")

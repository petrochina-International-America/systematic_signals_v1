"""
/api/db — serve the published snapshot (data.readback) instead of live compute.

One route per migrating component, each returning the SAME JSON shape as its
live counterpart so the frontend can flip a component between sources with a
config flag and nothing else:

    live (in-memory compute)            published snapshot (DB)
    ------------------------------      -------------------------------
    /api/signals/snapshot               /api/db/signals/snapshot
    /api/signals/spreads                /api/db/signals/spreads
    /api/signals/top-performers         /api/db/signals/top-performers
    /api/levels/proximity               /api/db/levels/proximity

Plus /api/db/meta — the permanent freshness guard: v_latest_date + the last
publish_run's status, and a server-computed `stale` flag so the frontend can
surface "data as of <date>" / a staleness warning instead of silently
rendering an old snapshot (the live API's freshness came for free; this
replaces it).

Reads systematic.* by default; set READBACK_SCHEMA=us_analysts when this
process should serve the replicated landing schema instead.
"""

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException

router = APIRouter()


def _guard(payload):
    """Published-snapshot routes 503 (not 200-with-empty) when nothing has
    ever been published — an unmistakable signal during the migration."""
    from data import readback
    if readback.latest_date() is None:
        raise HTTPException(503, "no successful publish_run in the DB — "
                                 "run data.publish (or POST /api/admin/publish)")
    return payload


@router.get("/signals/snapshot")
def db_signal_snapshot():
    from data import readback
    return _guard(readback.signal_snapshot())


@router.get("/signals/spreads")
def db_spread_snapshot():
    from data import readback
    return _guard(readback.spread_snapshot())


@router.get("/signals/top-performers")
def db_top_performers(n: int = 5):
    from data import readback
    return _guard(readback.top_performers(n))


@router.get("/levels/proximity")
def db_proximity(tenor: int = 1):
    """Published Levels snapshot for one selector tenor. tenors_available
    reflects what this snapshot actually holds — snapshots published before
    the tenor dimension only carry 1, and the frontend sizes (or hides) the
    M1–M4 selector from it rather than 404ing on a stale link."""
    from data import readback
    _guard(None)
    available = readback.levels_tenors()
    tenor = int(tenor)
    if tenor not in available:
        raise HTTPException(404, f"tenor {tenor} not in published snapshot "
                                 f"(available: {available})")
    payload = readback.proximity(tenor=tenor)
    payload["tenor"] = tenor
    payload["tenors_available"] = available
    return payload


@router.get("/lab/runs")
def db_lab_runs():
    """Index of stored canonical-run results (run_key + label per run).
    Only these keys resolve on the /api/db/lab/* routes — arbitrary lab
    parameters are compute, not storage, and stay on the live /api/lab."""
    from data import readback
    return _guard(readback.lab_runs())


def _lab_or_404(fn, key):
    payload = fn(key)
    if payload is None:
        raise HTTPException(404, "not a stored canonical run — see "
                                 "/api/db/lab/runs; arbitrary params need the "
                                 "live /api/lab")
    return payload


@router.get("/lab/result/{key:path}")
def db_lab_result(key: str):
    from data import readback
    return _lab_or_404(readback.lab_result, key)


@router.get("/lab/diagnostics/{key:path}")
def db_lab_diagnostics(key: str):
    from data import readback
    return _lab_or_404(readback.lab_diagnostics, key)


@router.get("/lab/split-metrics/{key:path}")
def db_lab_split_metrics(key: str):
    from data import readback
    return _lab_or_404(readback.lab_split_metrics, key)


def _prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:      # Sat/Sun
        d -= timedelta(days=1)
    return d


@router.get("/meta")
def db_meta():
    """Freshness of the published snapshot.

    stale=true when the latest ok snapshot is older than the last completed
    weekday — i.e. the morning publish that should have happened, didn't (or
    failed). Weekday logic only; an exchange holiday can produce a one-day
    false alarm, which is the acceptable side of that trade.
    """
    from data import readback
    from data.db import query_df

    as_of = readback.latest_date()
    runs = query_df(
        f"SELECT run_id, as_of_date, status, finished_at, error "
        f"FROM {readback._SCHEMA}.publish_run ORDER BY run_id DESC LIMIT 1"
    )
    last_run = None
    if not runs.empty:
        r = runs.iloc[0]
        last_run = {
            "run_id": int(r["run_id"]),
            "as_of_date": str(r["as_of_date"]),
            "status": str(r["status"]),
            "finished_at": str(r["finished_at"]) if r["finished_at"] is not None else None,
            "error": r["error"] if isinstance(r["error"], str) else None,
        }

    expected = _prev_weekday(date.today())
    stale = as_of is None or date.fromisoformat(as_of) < expected
    return {
        "schema": readback._SCHEMA,
        "as_of_date": as_of,
        "expected_date": str(expected),
        "stale": stale,
        "last_run": last_run,
    }

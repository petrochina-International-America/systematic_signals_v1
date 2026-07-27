"""
Round-trip test for the publish -> FlowsDB -> readback path.

Asserts that data.readback reproduces the live API payloads for the Signals and
Levels tabs exactly. If this passes, the systematic.* schema is lossless for
everything those two tabs render, and the shared work dashboard can serve them
from FlowsDB without calling this app at all.

Requires a reachable FlowsDB with the schema applied and a publish already run:

    py -3.14 -m data.publish --init

Skips (rather than fails) when the DB is unreachable or nothing is published
yet, so the rest of the suite still runs on a machine without FlowsDB.
"""

import json

import pytest

pytestmark = pytest.mark.integration


def _norm(obj):
    """Canonical JSON with -0.0 folded to 0.0.

    Postgres NUMERIC has no signed zero, so a -0.0 written into jsonb reads back
    as 0.0. Every consumer compares and renders them identically (-0.0 == 0.0 in
    both Python and JS), so this is a representation detail, not data loss.
    """
    def walk(o):
        if isinstance(o, float):
            return 0.0 if o == 0.0 else o
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return o

    return json.dumps(walk(obj), sort_keys=True, default=str)


@pytest.fixture(scope="module")
def published():
    """(as_of_date) of the latest successful publish, or skip."""
    try:
        from data import readback
        as_of = readback.latest_date()
    except Exception as exc:
        pytest.skip(f"FlowsDB unreachable: {type(exc).__name__}: {exc}")
    if as_of is None:
        pytest.skip("nothing published yet — run: py -3.14 -m data.publish --init")
    return as_of


@pytest.fixture(scope="module")
def warm():
    from data import loader
    if not loader.loaded_commodities():
        loader.warm_up()
    return loader


def test_signal_snapshot_roundtrip(published, warm):
    from api.signals import signal_snapshot
    from data import readback

    db = readback.signal_snapshot(published)
    db.pop("as_of_date")
    assert _norm(db) == _norm(signal_snapshot())


def test_spread_snapshot_roundtrip(published, warm):
    from api.signals import spread_snapshot
    from data import readback

    # Explicit None — bare call would pass FastAPI Query() objects.
    live = spread_snapshot(lookback=None, threshold=None)
    assert _norm(readback.spread_snapshot(published)) == _norm(live)


@pytest.mark.parametrize("section", ["groups", "spreads", "hot", "recent_trades"])
def test_proximity_roundtrip(published, warm, section):
    from api.levels import proximity
    from data import readback

    live = proximity()
    db = readback.proximity(published)
    assert _norm(db[section]) == _norm(live[section])


def test_top_performers_ranking_is_published(published):
    """The ranking bar must come back ordered and non-empty — a NULL-rank bug
    would silently render an empty bar rather than erroring."""
    from data import readback

    bars = readback.top_performers(5, published)
    assert bars["top_1y"], "no ranked 1Y performers published"
    assert bars["top_alltime"], "no ranked all-time performers published"
    for key, field in (("top_1y", "sharpe_1y"), ("top_alltime", "sharpe_all")):
        values = [r[field] for r in bars[key]]
        assert values == sorted(values, reverse=True), f"{key} not ranked by {field}"


def test_publish_is_idempotent(published, warm):
    """Re-publishing the same trading date updates rows instead of duplicating
    them — the whole design rests on as_of_date being the grain."""
    from data.db import query_df
    from data import publish

    before = query_df(
        "SELECT count(*) AS n FROM systematic.signal_outright WHERE as_of_date = :d",
        {"d": published},
    )["n"].iloc[0]

    publish.publish(as_of=published)

    after = query_df(
        "SELECT count(*) AS n FROM systematic.signal_outright WHERE as_of_date = :d",
        {"d": published},
    )["n"].iloc[0]

    assert before == after

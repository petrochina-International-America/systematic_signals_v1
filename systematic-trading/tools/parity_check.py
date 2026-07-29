"""
tools/parity_check.py — API↔DB parity harness for the migration off the
internal API onto the published schema (systematic.* locally, us_analysts on
the replicated DB).

For each component it pulls the SAME payload from both sides:

    component          API (live compute, HTTP)          DB (readback)
    ----------------   -------------------------------   -------------------------
    signals_snapshot   GET /api/signals/snapshot         readback.signal_snapshot
    top_performers     GET /api/signals/top-performers   readback.top_performers
    signals_spreads    GET /api/signals/spreads          readback.spread_snapshot
    levels             GET /api/levels/proximity?tenor=1  readback.proximity

then diffs field-by-field and writes one report per component under
audit_artifacts/parity/<as_of_date>/.

Like-for-like gate: the API computes from its in-memory price store; the DB
holds the last published snapshot. The harness refuses to run when
/api/health latest_data_date != <schema>.v_latest_date — that comparison
would flag price-generation skew, not publisher bugs. Re-warm the API
(POST /api/admin/reload) or run right after the morning publish.

Whitelisted (expected, acceptable) differences — everything else is a
stop-the-migration finding:

    rounding         sharpe_1y / sharpe_all compared at 2dp (publish stores
                     full precision, the API bar rounds for display).
    null-vs-missing  DB NULL where the API omits the key (either direction).
    db-metadata      DB payloads carry as_of_date; the live API does not.
    universe         DB persists the full canonical-run universe; the API
                     ranking bar may exclude unverified groups. Handled by
                     comparing the top-N lists both sides produce, not row
                     counts. (_UNVERIFIED_* are currently empty, so top-N
                     must match exactly.)

Usage (from systematic-trading/):
    py -3.14 -m tools.parity_check
    py -3.14 -m tools.parity_check --schema us_analysts --api-base http://localhost:8002
    py -3.14 -m tools.parity_check --components levels top_performers
    py -3.14 -m tools.parity_check --allow-date-mismatch   # annotate, don't gate

Exit codes: 0 all compared components PASS, 1 any DIFF, 75 date gate tripped.
"""

import argparse
import csv
import json
import math
import os
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_HERE)                      # systematic-trading/
_REPO = os.path.dirname(_PROJECT)                      # repo root
sys.path.insert(0, _PROJECT)
sys.path.insert(0, _REPO)

import requests

MISSING = object()          # sentinel: key absent (vs present-as-None)

# Fields stored rounded/full-precision on exactly one side: compare at N dp.
_ROUND_FIELDS = {"sharpe_1y": 2, "sharpe_all": 2}

# Keys the DB payload adds that the live API never had — metadata, not data.
_DB_METADATA_KEYS = {"as_of_date"}

_FLOAT_TOL = 1e-9           # float↔NUMERIC round-trip noise, not a real diff


# ── diff engine ───────────────────────────────────────────────────────────────


def _leaf_field(path: str) -> str:
    tail = path.rsplit(".", 1)[-1]
    return tail.split("[", 1)[0]


def _num_eq(a, b, tol=_FLOAT_TOL) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return False
    if math.isnan(fa) or math.isnan(fb):
        return False
    return abs(fa - fb) <= tol * max(1.0, abs(fa), abs(fb))


def _fmt(v) -> str:
    if v is MISSING:
        return "<missing>"
    s = json.dumps(v, default=str, ensure_ascii=False)
    return s if len(s) <= 200 else s[:197] + "..."


class Differ:
    def __init__(self):
        self.rows = []      # dicts: field, api_value, db_value, status, reason
        self.matches = 0

    def _row(self, path, a, b, status, reason=""):
        self.rows.append({"field": path, "api_value": _fmt(a),
                          "db_value": _fmt(b), "status": status,
                          "reason": reason})

    def diff(self, path, api, db):
        # ── missing vs present ──
        if api is MISSING or db is MISSING:
            present = db if api is MISSING else api
            if present is None:
                self._row(path, api, db, "WHITELISTED", "null-vs-missing")
            elif api is MISSING and _leaf_field(path) in _DB_METADATA_KEYS:
                self._row(path, api, db, "WHITELISTED", "db-metadata")
            else:
                self._row(path, api, db, "DIFF",
                          "api-only field" if db is MISSING else "db-only field")
            return

        # ── null handling ──
        if api is None or db is None:
            if api is None and db is None:
                self.matches += 1
            else:
                self._row(path, api, db, "DIFF", "null-vs-value")
            return

        # ── containers ──
        if isinstance(api, dict) and isinstance(db, dict):
            for k in list(api.keys()) + [k for k in db if k not in api]:
                self.diff(f"{path}.{k}" if path else str(k),
                          api.get(k, MISSING), db.get(k, MISSING))
            return

        if isinstance(api, list) and isinstance(db, list):
            key = self._align_key(api, db)
            if key:
                self._diff_keyed_lists(path, api, db, key)
                return
            if len(api) != len(db):
                self._row(path, f"len={len(api)}", f"len={len(db)}",
                          "DIFF", "list length")
                return
            for i, (x, y) in enumerate(zip(api, db)):
                self.diff(f"{path}[{i}]", x, y)
            return

        if isinstance(api, (dict, list)) != isinstance(db, (dict, list)):
            self._row(path, api, db, "DIFF", "type mismatch")
            return

        # ── scalars ──
        if api == db or _num_eq(api, db):
            self.matches += 1
            return
        dp = _ROUND_FIELDS.get(_leaf_field(path))
        if dp is not None and _num_eq(api, db, tol=0.5 * 10 ** -dp + 1e-12):
            self._row(path, api, db, "WHITELISTED", f"rounding-{dp}dp")
            return
        self._row(path, api, db, "DIFF", "value mismatch")

    @staticmethod
    def _align_key(api, db):
        """Natural key for lists of dicts, so reorderings diff by identity."""
        for key in ("commodity", "pair", "label", "instrument"):
            if (api and db
                    and all(isinstance(x, dict) and x.get(key) is not None for x in api)
                    and all(isinstance(x, dict) and x.get(key) is not None for x in db)):
                a_keys = [x[key] for x in api]
                d_keys = [x[key] for x in db]
                if len(set(a_keys)) == len(a_keys) and len(set(d_keys)) == len(d_keys):
                    return key
        return None

    def _diff_keyed_lists(self, path, api, db, key):
        a_map = {x[key]: x for x in api}
        d_map = {x[key]: x for x in db}
        a_order = [x[key] for x in api]
        d_order = [x[key] for x in db]
        if a_order != d_order:
            self._row(path + ".<order>", a_order, d_order, "DIFF", f"ordering by {key}")
        for k in a_order + [k for k in d_order if k not in a_map]:
            self.diff(f"{path}[{key}={k}]",
                      a_map.get(k, MISSING), d_map.get(k, MISSING))


# ── component fetchers ────────────────────────────────────────────────────────


def _api_get(base, route):
    r = requests.get(base + route, timeout=300)
    r.raise_for_status()
    return r.json()


def fetch_pairs(base, n):
    from data import readback
    return {
        "signals_snapshot": (
            lambda: _api_get(base, "/api/signals/snapshot"),
            lambda: readback.signal_snapshot(),
        ),
        "top_performers": (
            lambda: _api_get(base, f"/api/signals/top-performers?n={n}"),
            lambda: readback.top_performers(n),
        ),
        "signals_spreads": (
            lambda: _api_get(base, "/api/signals/spreads"),
            lambda: readback.spread_snapshot(),
        ),
        "levels": (
            lambda: _api_get(base, "/api/levels/proximity?tenor=1"),
            # Mirror the /api/db/levels/proximity wrapper — the frontend gets
            # the endpoint's payload, and the wrapper is part of that path.
            lambda: {**readback.proximity(), "tenor": 1},
        ),
    }


# ── reporting ─────────────────────────────────────────────────────────────────


def write_report(out_dir, component, differ, meta):
    os.makedirs(out_dir, exist_ok=True)
    n_diff = sum(1 for r in differ.rows if r["status"] == "DIFF")
    n_wl = sum(1 for r in differ.rows if r["status"] == "WHITELISTED")
    verdict = "PASS" if n_diff == 0 else "FAIL"

    csv_path = os.path.join(out_dir, f"parity_{component}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["component", "field", "api_value",
                                           "db_value", "status", "reason"])
        w.writeheader()
        for r in differ.rows:
            w.writerow({"component": component, **r})

    md_path = os.path.join(out_dir, f"parity_{component}.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(f"# Parity report — {component}\n\n")
        fh.write(f"- run at: {meta['run_at']}\n")
        fh.write(f"- as_of_date (both sides): {meta['as_of']}\n")
        fh.write(f"- api: {meta['api_base']}   db: {meta['schema']}.*\n")
        if meta.get("date_mismatch"):
            fh.write(f"- **WARNING: snapshot dates differ** — api={meta['api_date']} "
                     f"db={meta['db_date']}; diffs below may be price-generation "
                     f"skew, not publisher bugs\n")
        fh.write(f"\n**Verdict: {verdict}** — {differ.matches} fields match, "
                 f"{n_wl} whitelisted, {n_diff} DIFF\n\n")
        if differ.rows:
            fh.write("| field | api_value | db_value | status | reason |\n")
            fh.write("|---|---|---|---|---|\n")
            for r in differ.rows:
                fh.write("| {field} | {api_value} | {db_value} | {status} | "
                         "{reason} |\n".format(**{
                             k: str(v).replace("|", "\\|") for k, v in r.items()}))
        else:
            fh.write("No differences of any kind — payloads identical.\n")
    return verdict, differ.matches, n_wl, n_diff


# ── main ──────────────────────────────────────────────────────────────────────


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--api-base", default=os.getenv("PARITY_API_BASE",
                                                    "http://localhost:8002"))
    ap.add_argument("--schema", default=os.getenv("READBACK_SCHEMA", "systematic"),
                    choices=["systematic", "us_analysts"])
    ap.add_argument("--n", type=int, default=15,
                    help="top-performers depth (frontend requests 15)")
    ap.add_argument("--components", nargs="*",
                    choices=["signals_snapshot", "top_performers",
                             "signals_spreads", "levels"],
                    help="default: all")
    ap.add_argument("--allow-date-mismatch", action="store_true",
                    help="run even when api/db snapshot dates differ (annotated)")
    ap.add_argument("--out", default=os.path.join(_REPO, "audit_artifacts", "parity"))
    args = ap.parse_args(argv)

    from data import readback
    readback.set_schema(args.schema)

    # ── like-for-like gate ──
    health = _api_get(args.api_base, "/api/health")
    api_date = health.get("latest_data_date")
    db_date = readback.latest_date()
    mismatch = api_date != db_date
    if mismatch and not args.allow_date_mismatch:
        print(f"DATE GATE: api latest_data_date={api_date} but "
              f"{args.schema}.v_latest_date={db_date}.\n"
              f"Comparing live-vs-stale is not a parity test. Either:\n"
              f"  * POST {args.api_base}/api/admin/reload  (re-warm the API), or\n"
              f"  * run right after the morning publish, or\n"
              f"  * pass --allow-date-mismatch to proceed with annotation.")
        return 75

    meta = {"run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "as_of": db_date, "api_base": args.api_base, "schema": args.schema,
            "api_date": api_date, "db_date": db_date, "date_mismatch": mismatch}
    out_dir = os.path.join(args.out, str(db_date))

    pairs = fetch_pairs(args.api_base, args.n)
    components = args.components or list(pairs)

    summary, any_fail = [], False
    for comp in components:
        api_fn, db_fn = pairs[comp]
        print(f"[{comp}] fetching API side...")
        api_payload = api_fn()
        print(f"[{comp}] fetching DB side ({args.schema})...")
        db_payload = db_fn()
        d = Differ()
        d.diff("", api_payload, db_payload)
        verdict, n_match, n_wl, n_diff = write_report(out_dir, comp, d, meta)
        any_fail |= verdict == "FAIL"
        summary.append((comp, verdict, n_match, n_wl, n_diff))
        print(f"[{comp}] {verdict}: {n_match} match / {n_wl} whitelisted / {n_diff} DIFF")

    with open(os.path.join(out_dir, "SUMMARY.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# Parity summary — as_of {db_date}\n\n"
                 f"run at {meta['run_at']}, api {args.api_base}, "
                 f"schema {args.schema}\n\n")
        fh.write("| component | verdict | match | whitelisted | DIFF |\n|---|---|---|---|---|\n")
        for row in summary:
            fh.write("| {} | {} | {} | {} | {} |\n".format(*row))

    print(f"\nreports: {out_dir}")
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())

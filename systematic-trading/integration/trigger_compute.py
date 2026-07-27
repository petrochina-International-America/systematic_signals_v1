"""
trigger_compute.py — scheduler hook for the SystematicTrading compute step.

Drop this into the pipeline repo and call it AFTER the Bloomberg -> prices_daily
load commits and BEFORE the FlowsDB -> {dashboard, us_analysts} fan-out:

    Bloomberg -> prices_daily -> [ trigger_compute ] -> systematic.* -> fan-out

It POSTs the SystematicTrading API's publish endpoint, which re-pulls prices,
recomputes every Signals/Levels surface, and upserts the systematic.* schema in
FlowsDB. The endpoint is synchronous and takes ~2 min; nothing here polls.

Exit codes (also returned by run() as a status string) let the scheduler branch:

    0  published   — systematic.* is fresh for a new trading date -> run the fan-out
    75 not_ready   — prices have not advanced yet (HTTP 409). Not an error:
                     the upstream load probably has not committed. Skip the
                     fan-out and let the next scheduled tick retry. 75 is
                     EX_TEMPFAIL so a wrapping job can treat it as "retry later".
    1  failed      — the compute step errored. Alert, but DO NOT block or roll
                     back the price leg — prices are independent and matter more.

Config via env (all optional except the base URL in production):
    COMPUTE_API_BASE   e.g. http://systematic-host:8002   (default localhost:8002)
    COMPUTE_TIMEOUT_S  client read timeout, seconds        (default 400)
    COMPUTE_REQUIRE_FRESH  "0" to disable the staleness guard (default on)
"""

import os
import sys

import requests


def run(base_url: str | None = None,
        timeout_s: float | None = None,
        require_fresh: bool | None = None,
        logger=print) -> tuple[str, dict]:
    """
    Trigger the publish. Returns (status, detail) where status is one of
    "published" | "not_ready" | "failed". Never raises — the scheduler decides
    what a failure means for the rest of the run.
    """
    base = (base_url or os.getenv("COMPUTE_API_BASE", "http://localhost:8002")).rstrip("/")
    timeout = timeout_s if timeout_s is not None else float(os.getenv("COMPUTE_TIMEOUT_S", "400"))
    if require_fresh is None:
        require_fresh = os.getenv("COMPUTE_REQUIRE_FRESH", "1") != "0"

    url = f"{base}/api/admin/publish"
    params = {"require_fresh": "true" if require_fresh else "false"}

    try:
        resp = requests.post(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        logger(f"[compute] request failed: {type(exc).__name__}: {exc}")
        return "failed", {"error": str(exc)}

    if resp.status_code == 200:
        body = resp.json()
        logger(f"[compute] published as_of={body.get('as_of_date')} "
               f"run_id={body.get('run_id')} rows={body.get('rows')}")
        return "published", body

    if resp.status_code == 409:
        # Staleness guard tripped — prices have not advanced past the last
        # successful publish. Expected when this fires before the price load.
        detail = _detail(resp)
        logger(f"[compute] not ready — {detail}")
        return "not_ready", {"detail": detail}

    detail = _detail(resp)
    logger(f"[compute] publish failed: HTTP {resp.status_code} — {detail}")
    return "failed", {"status_code": resp.status_code, "detail": detail}


def _detail(resp: "requests.Response") -> str:
    try:
        return resp.json().get("detail", resp.text)
    except ValueError:
        return resp.text[:500]


_EXIT = {"published": 0, "not_ready": 75, "failed": 1}

if __name__ == "__main__":
    status, _ = run()
    sys.exit(_EXIT[status])

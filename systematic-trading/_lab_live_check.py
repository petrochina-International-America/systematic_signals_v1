"""One-shot live check of the rewritten app's callbacks over HTTP."""
import sys, time, json
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import requests

BASE = "http://127.0.0.1:8050"

for _ in range(60):
    try:
        requests.get(BASE, timeout=2)
        break
    except Exception:
        time.sleep(2)
else:
    sys.exit("server never came up")
print("server up")


def call(outputs, inputs, state=None):
    if isinstance(outputs, str):
        outputs = [outputs]
    body = {
        # Dash multi-output callback key: ..id.prop...id.prop..
        "output": (".." + "...".join(outputs) + "..") if len(outputs) > 1 else outputs[0],
        "outputs": [{"id": o.split(".")[0], "property": o.split(".")[1]} for o in outputs]
        if len(outputs) > 1 else {"id": outputs[0].split(".")[0], "property": outputs[0].split(".")[1]},
        "inputs": inputs,
        "changedPropIds": [i["id"] + "." + i["property"] for i in inputs],
        "state": state or [],
    }
    r = requests.post(f"{BASE}/_dash-update-component", json=body, timeout=300)
    if r.status_code != 200:
        print("  FAIL", r.status_code, r.text[:1500])
        return None
    return r.json()


# 1. Route each page
for path in ["/cot-flows", "/signals", "/levels", "/strategy-lab"]:
    resp = call("page-outlet.children",
                [{"id": "url", "property": "pathname", "value": path}],
                state=[{"id": "url", "property": "search", "value": ""}])
    print(f"route {path}:", "OK" if resp else "FAIL")

# 2. Commodity store sync
resp = call("commodity-store.data",
            [{"id": "commodity-selector", "property": "value", "value": "Brent"}])
print("store sync:", resp["response"]["commodity-store"]["data"] if resp else "FAIL")

# 3. COT page content
resp = call("cot-page-content.children",
            [{"id": "commodity-store", "property": "data", "value": "Brent"}])
print("cot page render:", "OK" if resp else "FAIL")

# 4. Lab compute (Momentum)
lab_inputs = [
    {"id": "commodity-store", "property": "data", "value": "WTI"},
    {"id": "lab-strategy", "property": "value", "value": "Momentum"},
    {"id": "lab-mom-tier", "property": "value", "value": "Fast"},
    {"id": "lab-mom-fast", "property": "value", "value": 5},
    {"id": "lab-mom-slow", "property": "value", "value": 60},
    {"id": "lab-carry-front", "property": "value", "value": "F4"},
    {"id": "lab-carry-end", "property": "value", "value": "F15"},
    {"id": "lab-sa-pair", "property": "value", "value": "WTI / Brent"},
    {"id": "lab-sa-lookback", "property": "value", "value": 60},
    {"id": "lab-sa-entry", "property": "value", "value": 1.0},
    {"id": "lab-sa-exit", "property": "value", "value": 0.0},
    {"id": "lab-sa-hedge", "property": "value", "value": "50/50"},
    {"id": "lab-sa-tenor", "property": "value", "value": "Prompt"},
    {"id": "lab-cot-signal", "property": "value", "value": "Follow the Flow"},
    {"id": "lab-cot-fast", "property": "value", "value": 4},
    {"id": "lab-cot-slow", "property": "value", "value": 16},
    {"id": "lab-cot-threshold", "property": "value", "value": 20},
    {"id": "lab-vol-target", "property": "value", "value": 15},
    {"id": "lab-vol-window", "property": "value", "value": 120},
]
resp = call(["lab-store.data", "url.search", "lab-note.children", "lab-note.style"], lab_inputs)
if resp:
    store_data = resp["response"]["lab-store"]["data"]
    print("lab compute:", "OK key" if "key" in store_data else f"ERROR {store_data}")
    print("url search:", resp["response"]["url"]["search"])

    # 5. Display callbacks fed from the store
    for out in ["lab-price-chart.figure", "lab-mtm-chart.figure"]:
        r2 = call(out, [{"id": "lab-store", "property": "data", "value": store_data}])
        print(f"{out}:", "OK" if r2 else "FAIL")
    r2 = call(["lab-diagnostics.data", "lab-diagnostics.columns",
               "lab-metrics-table.data", "lab-metrics-table.columns"],
              [{"id": "lab-store", "property": "data", "value": store_data}])
    print("lab tables:", "OK" if r2 else "FAIL")
    r2 = call(["lab-metric-sharpe.children", "lab-metric-cagr.children",
               "lab-metric-drawdown.children", "lab-metric-pnl.children"],
              [{"id": "lab-store", "property": "data", "value": store_data}])
    print("lab cards:", {k: v["children"] for k, v in r2["response"].items()} if r2 else "FAIL")

# 6. Stat-Arb compute via callback
sa_inputs = [dict(i) for i in lab_inputs]
sa_inputs[1] = {"id": "lab-strategy", "property": "value", "value": "Stat-Arb"}
resp = call(["lab-store.data", "url.search", "lab-note.children", "lab-note.style"], sa_inputs)
if resp:
    store_data = resp["response"]["lab-store"]["data"]
    print("statarb compute:", "OK" if "key" in store_data else f"ERROR {store_data}")
    r2 = call("lab-price-chart.figure", [{"id": "lab-store", "property": "data", "value": store_data}])
    print("statarb spread fig:", "OK" if r2 else "FAIL")
    # sweep: button click
    r2 = call("lab-sweep-chart.figure",
              [{"id": "lab-sweep-btn", "property": "n_clicks", "value": 1},
               {"id": "lab-store", "property": "data", "value": store_data}])
    print("statarb sweep fig:", "OK" if r2 else "FAIL")

# 7. Signals drill-down (pattern-matching ALL input: list-of-lists wire format)
cell_id = {"item": "WTI", "strat": "Momentum", "type": "sig-cell"}
body = {
    "output": "signal-drill.children",
    "outputs": {"id": "signal-drill", "property": "children"},
    "inputs": [[{"id": cell_id, "property": "n_clicks", "value": 1}]],
    "changedPropIds": [json.dumps(cell_id, separators=(",", ":"), sort_keys=True) + ".n_clicks"],
    "state": [],
}
r = requests.post(f"{BASE}/_dash-update-component", json=body, timeout=300)
print("signals drill-down:", "OK" if r.status_code == 200 else f"FAIL {r.status_code} {r.text[:800]}")

print("LIVE CHECK DONE")

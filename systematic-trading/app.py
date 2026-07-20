import sys
import os

# Ensure project root is on the path so sub-packages resolve correctly
_PROJECT_ROOT = os.path.dirname(__file__)
sys.path.insert(0, _PROJECT_ROOT)

# The `energy` signal library lives as a sibling directory (one level up) —
# referenced in place rather than vendored, so research and app share one copy.
sys.path.insert(0, os.path.dirname(_PROJECT_ROOT))

import dash
from dash import html, dcc, Input, Output, State

# Warm the in-memory data store once at startup.  All price and expiry data
# is pulled from FlowsDB here; nothing else in the app queries the DB for prices.
import data.loader as _loader
_loader.warm_up()

from layout.sidebar import sidebar
from layout.topbar import topbar, DEFAULT_COMMODITY
import pages.cot_flows as cot_flows
import pages.signals as signals
import pages.levels as levels
import pages.strategy_lab as strategy_lab

app = dash.Dash(
    __name__,
    suppress_callback_exceptions=True,
    title="SystematicTrading",
    update_title=None,
)
server = app.server  # for deployment via gunicorn/waitress

_PAGE_TITLES = {
    "/": "COT Flows",
    "/cot-flows": "COT Flows",
    "/signals": "Signals",
    "/levels": "Levels",
    "/strategy-lab": "Strategy Lab",
}

app.layout = html.Div(
    id="app-shell",
    children=[
        dcc.Location(id="url", refresh=False),
        sidebar(),
        html.Div(
            className="main-area",
            children=[
                # Topbar lives in the shell (single mount) — the commodity
                # selector is never re-created on navigation, fixing the old
                # shared-ID fragility across pages.
                topbar(),
                html.Div(id="page-outlet"),
            ],
        ),
        # Cross-page state: pages read commodity-store, never the selector
        # directly. One-way selector → store sync below (selector persistence
        # handles reload seeding, avoiding a circular dependency).
        dcc.Store(id="commodity-store", data=DEFAULT_COMMODITY),
    ],
)


@app.callback(
    Output("commodity-store", "data"),
    Input("commodity-selector", "value"),
)
def _sync_commodity_store(value: str):
    return value or DEFAULT_COMMODITY


@app.callback(
    Output("topbar-title", "children"),
    Input("url", "pathname"),
)
def _update_title(pathname: str):
    return _PAGE_TITLES.get(pathname, "SystematicTrading")


@app.callback(
    Output("page-outlet", "children"),
    Input("url", "pathname"),
    State("url", "search"),
)
def route(pathname: str, search: str):
    if pathname in ("/", "/cot-flows"):
        return cot_flows.layout()
    if pathname == "/signals":
        return signals.layout()
    if pathname == "/levels":
        return levels.layout()
    if pathname == "/strategy-lab":
        # search carries bookmarkable lab parameters (?strategy=...&commodity=...)
        return strategy_lab.layout(search)
    return html.Div(
        className="page-content",
        style={"color": "#9ba3b2", "padding": "40px"},
        children=f"404 — no page at {pathname}",
    )


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=8050)

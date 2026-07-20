"""
pages/signals.py — live signal monitor with drill-down.

The grids show the latest Momentum/Carry direction per commodity and
equal-weighted basket (from data.signals' 5-min-cached live snapshot).
Clicking any commodity cell drills into the full default-parameter backtest
for that (commodity, strategy) — served from the Strategy Lab compute cache,
so repeated clicks are instant.
"""
from dash import html, dcc, Input, Output, callback, ctx, ALL, no_update

from components.signal_grid import signal_grid
from components.lab_charts import price_space_figure, mtm_figure
from data.signals import get_signal_snapshot, PRODUCT_GROUPS, STRATEGIES
from data import lab


def _grid_panel(title: str, row_labels: list[str], lookup, clickable: bool) -> html.Div:
    cell_id = None
    if clickable:
        runnable = set(lab.available_commodities())

        def cell_id(row, col):  # noqa: F811 — intentional closure rebind
            if row in runnable and col in lab.STRATEGIES:
                return {"type": "sig-cell", "item": row, "strat": col}
            return None

    return html.Div(
        className="table-panel",
        children=[
            html.Div(title, className="panel-heading"),
            html.Div(
                className="signal-grid-wrap",
                children=signal_grid(row_labels, STRATEGIES, lookup, cell_id=cell_id),
            ),
        ],
    )


def layout() -> html.Div:
    snapshot = get_signal_snapshot()

    def lookup(row_label, col_label):
        return snapshot[(row_label, col_label)]

    basket_panel = _grid_panel(
        "Basket Monitor — Equal-Weighted by Product Group",
        list(PRODUCT_GROUPS.keys()),
        lookup,
        clickable=False,
    )

    group_panels = [
        _grid_panel(f"{group} — By Commodity", commodities, lookup, clickable=True)
        for group, commodities in PRODUCT_GROUPS.items()
    ]

    return html.Div(
        className="page-container",
        children=[
            html.Div(
                className="page-content",
                children=[
                    basket_panel,
                    html.Div(id="signal-drill"),
                    *group_panels,
                ],
            ),
        ],
    )


@callback(
    Output("signal-drill", "children"),
    Input({"type": "sig-cell", "item": ALL, "strat": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def _drill_down(n_clicks):
    if isinstance(n_clicks, int):  # defensive: ALL normally delivers a list
        n_clicks = [n_clicks]
    if not any(n or 0 for n in n_clicks):
        return no_update
    trigger = ctx.triggered_id
    if not trigger:
        return no_update

    commodity, strategy = trigger["item"], trigger["strat"]
    try:
        key = lab.run_lab({"strategy": strategy, "commodity": commodity})
        result = lab.get_result(key)
    except Exception as e:
        return html.Div(className="placeholder-note",
                        children=f"Drill-down failed for {commodity} {strategy}: {e}")

    vol_target = lab.DEFAULTS["vol_target"]
    return html.Div(
        className="table-panel",
        children=[
            html.Div(
                className="panel-heading-row",
                children=[
                    html.Div(f"Drill-Down — {result['label']} (default parameters)",
                             className="panel-heading"),
                    dcc.Link("Open in Strategy Lab →",
                             href=f"/strategy-lab?strategy={strategy}",
                             className="panel-link"),
                ],
            ),
            html.Div(
                className="chart-grid-2",
                style={"padding": "12px"},
                children=[
                    dcc.Graph(figure=price_space_figure(result),
                              config={"displayModeBar": False}, style={"height": "440px"}),
                    dcc.Graph(figure=mtm_figure(result, vol_target=vol_target),
                              config={"displayModeBar": False}, style={"height": "440px"}),
                ],
            ),
        ],
    )

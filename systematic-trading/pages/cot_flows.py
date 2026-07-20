"""
pages/cot_flows.py — COT positioning monitor.

Reads the cross-page commodity from commodity-store (set by the shell topbar).
Data comes through data.cot's stable interface — synthetic today, cot_bbg
later, page unchanged either way.
"""
from dash import html, dcc, Input, Output, callback
from datetime import datetime, timedelta

from components.metric_card import metric_card
from components.positioning_table import positioning_table
from components.charts import cot_chart, sentiment_figure, percentile_histogram
from data.cot import get_cot, get_cot_snapshot, follow_the_flow, fade_the_crowd, is_synthetic
from data.prices import get_prices

_END = datetime.today().strftime("%Y-%m-%d")
_START = (datetime.today() - timedelta(days=3 * 365)).strftime("%Y-%m-%d")

# Default signal parameters for the monitor views (full parameter exploration
# lives in the Strategy Lab COT section).
_MA_FAST, _MA_SLOW = 4, 16
_SI_THRESHOLD = 20.0

_SNAPSHOT_COMMODITIES = ["WTI", "Brent", "Natgas", "RBOB", "ULSD", "Gasoil"]


def layout() -> html.Div:
    return html.Div(
        className="page-container",
        children=[
            html.Div(className="page-content", id="cot-page-content"),
        ],
    )


@callback(
    Output("cot-page-content", "children"),
    Input("commodity-store", "data"),
)
def render_cot(commodity: str):
    commodity = commodity or "WTI"

    cot_df = get_cot(commodity, _START, _END)
    latest = cot_df.iloc[-1]
    price_df = get_prices(commodity, _START, _END)
    snapshot_df = get_cot_snapshot(_SNAPSHOT_COMMODITIES)

    ma_df = follow_the_flow(cot_df, _MA_FAST, _MA_SLOW)
    si_df = fade_the_crowd(cot_df, _SI_THRESHOLD)

    mm_net = int(latest["mm_net"])
    mm_chg = int(latest["mm_net_change"])
    pct_rank = float(latest["percentile_rank"])
    crowding = str(latest["crowding_flag"])

    chg_color = "#639922" if mm_chg >= 0 else "#E24B4A"
    crowd_color = {"Crowded": "#E24B4A", "Washed": "#639922"}.get(crowding, "#378ADD")

    children = [
        html.Div(
            className="metric-row",
            children=[
                metric_card("MM Net Position", f"{mm_net:,}", "contracts"),
                metric_card("MM Net Change WoW",
                            f"{'+' if mm_chg >= 0 else ''}{mm_chg:,}",
                            "contracts", color=chg_color),
                metric_card("Percentile Rank", f"{pct_rank:.1f}th", "52-week"),
                metric_card("Crowding Flag", crowding, "", color=crowd_color),
            ],
        ),
        html.Div(
            className="chart-panel",
            children=[
                dcc.Graph(
                    figure=cot_chart(cot_df, price_df, commodity, ma_df=ma_df),
                    config={"displayModeBar": False},
                    style={"height": "380px"},
                ),
            ],
        ),
        html.Div(
            className="chart-grid-2",
            children=[
                html.Div(className="chart-panel", children=[
                    dcc.Graph(figure=sentiment_figure(si_df, _SI_THRESHOLD, commodity),
                              config={"displayModeBar": False}, style={"height": "320px"})]),
                html.Div(className="chart-panel", children=[
                    dcc.Graph(figure=percentile_histogram(cot_df, commodity),
                              config={"displayModeBar": False}, style={"height": "320px"})]),
            ],
        ),
        html.Div(
            className="table-panel",
            children=[
                html.Div("Positioning Snapshot — All Commodities", className="panel-heading"),
                positioning_table(snapshot_df),
            ],
        ),
    ]

    if is_synthetic():
        children.append(html.Div(
            className="placeholder-note",
            children="COT data is synthetic (cot_bbg table pending) — "
                     "layout and signal mechanics are final, values are not.",
        ))

    return children

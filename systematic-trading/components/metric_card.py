from dash import html


def metric_card(label: str, value: str, sub: str = "", color: str = "#378ADD", id: str | None = None) -> html.Div:
    """Reusable KPI card for the dark theme. Pass `id` to make `value` updatable via callback."""
    value_div = html.Div(value, className="metric-value", style={"color": color})
    if id is not None:
        value_div.id = id
    return html.Div(
        className="metric-card",
        children=[
            html.Div(label, className="metric-label"),
            value_div,
            html.Div(sub, className="metric-sub") if sub else None,
        ],
    )

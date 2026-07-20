# Shell page — confluence/levels engine not built yet. Layout and styling are
# final; replace _FAKE_LEVELS with the engine output when it exists.

from dash import html, dash_table

_FAKE_LEVELS = [
    {"Commodity": "WTI", "Level": "$78.50", "Type": "CTA Entry", "Sigma": "+1.0σ", "Positioning Context": "Neutral (48th pct)"},
    {"Commodity": "WTI", "Level": "$74.20", "Type": "CTA Stop", "Sigma": "-1.5σ", "Positioning Context": "Washed (<25th pct)"},
    {"Commodity": "Brent", "Level": "$83.10", "Type": "Signal Level", "Sigma": "+1.5σ", "Positioning Context": "Crowded (>75th pct)"},
    {"Commodity": "Brent", "Level": "$79.80", "Type": "CTA Entry", "Sigma": "+0.5σ", "Positioning Context": "Neutral (55th pct)"},
    {"Commodity": "Natgas", "Level": "$2.85", "Type": "Signal Level", "Sigma": "-2.0σ", "Positioning Context": "Washed (<25th pct)"},
]

_TYPE_COLORS = {
    "CTA Entry": "#378ADD",
    "CTA Stop": "#E24B4A",
    "Signal Level": "#EF9F27",
}


def layout() -> html.Div:
    style_data_conditional = [
        {
            "if": {"filter_query": f'{{Type}} = "{t}"', "column_id": "Type"},
            "color": color,
            "fontWeight": "600",
        }
        for t, color in _TYPE_COLORS.items()
    ]

    table = dash_table.DataTable(
        data=_FAKE_LEVELS,
        columns=[{"name": c, "id": c} for c in _FAKE_LEVELS[0].keys()],
        style_as_list_view=True,
        style_table={"overflowX": "auto"},
        style_header={
            "backgroundColor": "#1a1d27",
            "color": "#9ba3b2",
            "fontWeight": "600",
            "fontSize": "11px",
            "textTransform": "uppercase",
            "letterSpacing": "0.06em",
            "borderBottom": "1px solid #2d3142",
            "padding": "10px 14px",
        },
        style_cell={
            "backgroundColor": "#12151f",
            "color": "#d4dae6",
            "border": "none",
            "fontFamily": "Inter, system-ui, sans-serif",
            "fontSize": "13px",
            "padding": "10px 14px",
            "textAlign": "left",
        },
        style_data_conditional=style_data_conditional,
    )

    return html.Div(
        className="page-container",
        children=[
            html.Div(
                className="page-content",
                children=[
                    html.Div(
                        className="table-panel",
                        children=[
                            html.Div("Key Levels — All Commodities", className="panel-heading"),
                            table,
                        ],
                    ),
                    html.Div(
                        className="placeholder-note",
                        children="Shell page — confluence engine integration pending (hardcoded sample rows).",
                    ),
                ],
            ),
        ],
    )

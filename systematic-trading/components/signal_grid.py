from dash import html

_DIRECTION_COLORS = {
    "Long": "#639922",
    "Short": "#E24B4A",
    "Flat": "#9ba3b2",
}


def signal_cell(direction: str, sub_label: str | None = None) -> html.Div:
    """
    Direction chip for the monitor grid.

    direction  — "Long" / "Short" / "Flat" / "—"
    sub_label  — optional line below the direction (e.g. "+2.3σ" for spreads).
                 Pass None (or omit) for flat-price signals where only
                 direction matters.
    """
    color = _DIRECTION_COLORS.get(direction, "#9ba3b2")
    children = [html.Span(direction, className="signal-cell-direction", style={"color": color})]
    if sub_label:
        children.append(html.Span(sub_label, className="signal-cell-zscore"))
    return html.Div(className="signal-cell", children=children)


def signal_grid(
    row_labels: list[str],
    col_labels: list[str],
    cell_lookup,
    row_label_width: str = "160px",
    cell_id=None,
) -> html.Div:
    """
    Render a strategy-by-item grid.

    cell_lookup(row_label, col_label) -> (direction, sub_label)
    cell_id(row_label, col_label)     -> optional pattern-matching dict id;
        when provided the cell becomes a click target for drill-down callbacks.
    """
    header = html.Div(
        className="signal-grid-row signal-grid-header",
        children=[
            html.Div("", className="signal-grid-row-label", style={"width": row_label_width}),
            *[html.Div(c, className="signal-grid-col-label") for c in col_labels],
        ],
    )

    def _cell(row_label, col_label):
        kwargs = dict(
            children=signal_cell(*cell_lookup(row_label, col_label)),
            className="signal-grid-cell",
        )
        cid = cell_id(row_label, col_label) if cell_id else None
        if cid is not None:
            kwargs["id"] = cid
            kwargs["n_clicks"] = 0
            kwargs["className"] += " signal-grid-cell-clickable"
        return html.Div(**kwargs)

    rows = [
        html.Div(
            className="signal-grid-row",
            children=[
                html.Div(row_label, className="signal-grid-row-label",
                         style={"width": row_label_width}),
                *[_cell(row_label, col_label) for col_label in col_labels],
            ],
        )
        for row_label in row_labels
    ]

    return html.Div(className="signal-grid", children=[header, *rows])

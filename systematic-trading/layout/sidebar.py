from dash import html, dcc


_NAV_ITEMS = [
    {"label": "COT Flows", "href": "/cot-flows", "icon": "◈"},
    {"label": "Signals", "href": "/signals", "icon": "◉"},
    {"label": "Levels", "href": "/levels", "icon": "◧"},
    {"label": "Strategy Lab", "href": "/strategy-lab", "icon": "▣"},
]


def sidebar() -> html.Div:
    return html.Div(
        id="sidebar",
        className="sidebar",
        children=[
            html.Div(
                className="sidebar-brand",
                children=[
                    html.Span("ST", className="brand-mark"),
                    html.Span("SystematicTrading", className="brand-text"),
                ],
            ),
            html.Div(className="sidebar-divider"),
            html.Nav(
                className="sidebar-nav",
                children=[
                    dcc.Link(
                        href=item["href"],
                        className="nav-link",
                        children=[
                            html.Span(item["icon"], className="nav-icon"),
                            html.Span(item["label"], className="nav-label"),
                        ],
                    )
                    for item in _NAV_ITEMS
                ],
            ),
        ],
    )

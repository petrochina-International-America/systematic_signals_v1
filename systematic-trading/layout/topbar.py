"""
layout/topbar.py — app-shell topbar.

Mounted ONCE in app.py (not per page). This removes the old shared-ID
fragility where every page re-created its own "commodity-selector": there is
now exactly one selector instance, alive across navigations, synced to the
commodity-store in app.py. The page title is updated via callback on the URL.
"""
from dash import html, dcc
from datetime import datetime

_FALLBACK_COMMODITIES = ["WTI", "Brent", "Natgas"]
DEFAULT_COMMODITY = "WTI"


def _commodity_options() -> list[str]:
    """All commodities loaded into the price store (loader is warmed before layout)."""
    try:
        from data import loader
        names = loader.loaded_commodities()
        if names:
            return names
    except Exception:
        pass
    return _FALLBACK_COMMODITIES


def topbar() -> html.Div:
    commodities = _commodity_options()
    default = DEFAULT_COMMODITY if DEFAULT_COMMODITY in commodities else commodities[0]
    updated = datetime.now().strftime("Updated %d %b %Y %H:%M")

    return html.Div(
        className="topbar",
        children=[
            html.Div(
                className="topbar-left",
                children=[html.H1("", id="topbar-title", className="topbar-title")],
            ),
            html.Div(
                className="topbar-right",
                children=[
                    html.Span(updated, className="topbar-timestamp"),
                    dcc.Dropdown(
                        id="commodity-selector",
                        options=[{"label": c, "value": c} for c in commodities],
                        value=default,
                        clearable=False,
                        # persistence (not a store→selector callback) seeds the
                        # value on reload — avoids a circular dependency with
                        # the selector→store sync in app.py
                        persistence=True,
                        persistence_type="session",
                        className="commodity-dropdown",
                    ),
                ],
            ),
        ],
    )

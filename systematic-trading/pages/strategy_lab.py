"""
pages/strategy_lab.py — interactive backtest lab for all four strategies.

Callback architecture (no God callbacks):
    controls ──► _compute ──► dcc.Store("lab-store") holds only {key, params}
                              (full pandas results stay server-side in data.lab's
                              LRU cache — keyed, recomputable, never serialized
                              to the browser)
    lab-store ──► separate display callbacks: price-space figure, MTM figure,
                  metric cards, diagnostics + sample-split tables, sweep figure.

URL state: _compute also writes the normalized params into dcc.Location's
search string, so any lab configuration is bookmarkable/shareable; layout()
seeds the controls back from that query string.
"""
from urllib.parse import parse_qs, urlencode

import pandas as pd
from dash import html, dcc, dash_table, Input, Output, State, callback, ctx, no_update

from components.metric_card import metric_card
from components.lab_charts import price_space_figure, spread_figure, mtm_figure, sweep_heatmap
from components.charts import _LAYOUT_BASE
from data import lab

import plotly.graph_objects as go

_STRATEGY_OPTIONS = [{"label": s, "value": s} for s in lab.STRATEGIES]
_TIER_OPTIONS = [{"label": t, "value": t} for t in list(lab.MOMENTUM_TIERS) + ["Custom"]]
_PAIR_OPTIONS = [{"label": lab.pair_label(*p), "value": lab.pair_label(*p)} for p in lab.STAT_ARB_PAIRS]
_HEDGE_OPTIONS = [
    {"label": "50/50 notional", "value": "50/50"},
    {"label": "OLS β (rolling)", "value": "ols"},
]
_TENOR_OPTIONS = [{"label": k, "value": k} for k in lab.ROLL_TENORS]
_COT_SIGNAL_OPTIONS = [{"label": s, "value": s} for s in lab.COT_SIGNALS]

_NUMERIC_INT = {"fast", "slow", "lookback", "cot_fast", "cot_slow", "vol_window"}
_NUMERIC_FLOAT = {"entry", "exit", "cot_threshold", "vol_target"}

_TABLE_STYLE = dict(
    style_as_list_view=True,
    style_table={"overflowX": "auto"},
    style_header={
        "backgroundColor": "#1a1d27", "color": "#9ba3b2", "fontWeight": "600",
        "fontSize": "11px", "textTransform": "uppercase", "letterSpacing": "0.06em",
        "borderBottom": "1px solid #2d3142", "padding": "10px 14px", "textAlign": "center",
    },
    style_cell={
        "backgroundColor": "#12151f", "color": "#d4dae6", "border": "none",
        "fontFamily": "Inter, system-ui, sans-serif", "fontSize": "13px",
        "padding": "10px 14px", "textAlign": "right",
    },
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _fmt(value, spec: str) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    return format(value, spec)


def _params_from_search(search: str | None) -> dict:
    """Parse URL query string back into typed lab params (bookmarkable state)."""
    out = dict(lab.DEFAULTS)
    if not search:
        return out
    try:
        qs = parse_qs(search.lstrip("?"))
    except Exception:
        return out
    for k, vals in qs.items():
        if k not in out or not vals:
            continue
        v = vals[0]
        try:
            if k in _NUMERIC_INT:
                out[k] = int(float(v))
            elif k in _NUMERIC_FLOAT:
                out[k] = float(v)
            else:
                out[k] = v
        except (ValueError, TypeError):
            pass
    return out


def _placeholder_fig(text: str) -> go.Figure:
    fig = go.Figure()
    base = {k: v for k, v in _LAYOUT_BASE.items() if k not in ("xaxis", "yaxis", "hovermode")}
    fig.update_layout(
        **base,
        xaxis=dict(visible=False), yaxis=dict(visible=False),
        annotations=[dict(text=text, showarrow=False, font=dict(color="#9ba3b2", size=13))],
    )
    return fig


def _split_table_columns(df: pd.DataFrame) -> list[dict]:
    columns = [{"name": ["", "Sample"], "id": "Sample"}]
    for col in df.columns:
        if col == "Sample":
            continue
        group, metric = col.split("::", 1)
        group_label = "Price Space" if group == "PS" else "MTM"
        columns.append({"name": [group_label, metric], "id": col})
    return columns


def _control(label: str, component, control_id: str | None = None, style=None) -> html.Div:
    kwargs = dict(
        className="lab-control", style=style,
        children=[html.Label(label, className="lab-label"), component],
    )
    if control_id is not None:  # Dash rejects an explicit id=None
        kwargs["id"] = control_id
    return html.Div(**kwargs)


# ── layout ────────────────────────────────────────────────────────────────────

def layout(search: str | None = None) -> html.Div:
    p = _params_from_search(search)

    commodities = lab.available_commodities()
    fcols = lab.available_fcols(p["commodity"]) if p["commodity"] in commodities else []
    fcol_options = [{"label": c, "value": c} for c in fcols]
    carry_front = p["carry_front"] if p["carry_front"] in fcols else (fcols[0] if fcols else None)
    carry_end = p["carry_end"] if p["carry_end"] in fcols else (fcols[-1] if fcols else None)

    momentum_block = html.Div(
        id="lab-momentum-controls", className="lab-strategy-block",
        children=[
            _control("Speed Tier", dcc.Dropdown(
                id="lab-mom-tier", options=_TIER_OPTIONS, value=p["tier"], clearable=False)),
            _control("Custom Fast MA (days)", dcc.Slider(
                id="lab-mom-fast", min=1, max=60, step=1, value=p["fast"],
                marks={1: "1", 5: "5", 20: "20", 60: "60"},
                tooltip={"placement": "bottom", "always_visible": True}),
                control_id="lab-mom-fast-wrap"),
            _control("Custom Slow MA (days)", dcc.Slider(
                id="lab-mom-slow", min=5, max=250, step=5, value=p["slow"],
                marks={5: "5", 60: "60", 120: "120", 250: "250"},
                tooltip={"placement": "bottom", "always_visible": True}),
                control_id="lab-mom-slow-wrap"),
        ],
    )

    carry_block = html.Div(
        id="lab-carry-controls", className="lab-strategy-block", style={"display": "none"},
        children=[
            _control("Carry Legs (Front / End)", html.Div(
                className="lab-subrow",
                children=[
                    dcc.Dropdown(id="lab-carry-front", options=fcol_options, value=carry_front, clearable=False),
                    dcc.Dropdown(id="lab-carry-end", options=fcol_options, value=carry_end, clearable=False),
                ])),
        ],
    )

    statarb_block = html.Div(
        id="lab-statarb-controls", className="lab-strategy-block", style={"display": "none"},
        children=[
            _control("Pair", dcc.Dropdown(
                id="lab-sa-pair", options=_PAIR_OPTIONS, value=p["pair"], clearable=False)),
            _control("Lookback n (days)", dcc.Slider(
                id="lab-sa-lookback", min=5, max=250, step=5, value=p["lookback"],
                marks={5: "5", 20: "20", 60: "60", 120: "120", 250: "250"},
                tooltip={"placement": "bottom", "always_visible": True})),
            _control("Entry Threshold ε (σ)", dcc.Slider(
                id="lab-sa-entry", min=0.5, max=3.0, step=0.05, value=p["entry"],
                marks={0.5: "0.5", 1: "1.0", 2: "2.0", 3: "3.0"},
                tooltip={"placement": "bottom", "always_visible": True})),
            _control("Exit Threshold (σ, 0 = zero-cross)", dcc.Slider(
                id="lab-sa-exit", min=0.0, max=1.5, step=0.05, value=p["exit"],
                marks={0: "0", 0.5: "0.5", 1: "1.0", 1.5: "1.5"},
                tooltip={"placement": "bottom", "always_visible": True})),
            _control("Hedge Ratio", dcc.RadioItems(
                id="lab-sa-hedge", options=_HEDGE_OPTIONS, value=p["hedge"],
                className="lab-radio", inline=True)),
            _control("Roll Tenor", dcc.RadioItems(
                id="lab-sa-tenor", options=_TENOR_OPTIONS, value=p["roll_tenor"],
                className="lab-radio", inline=True)),
        ],
    )

    cot_block = html.Div(
        id="lab-cot-controls", className="lab-strategy-block", style={"display": "none"},
        children=[
            _control("COT Signal", dcc.RadioItems(
                id="lab-cot-signal", options=_COT_SIGNAL_OPTIONS, value=p["cot_signal"],
                className="lab-radio", inline=True)),
            _control("Fast MA (weeks)", dcc.Slider(
                id="lab-cot-fast", min=2, max=26, step=1, value=p["cot_fast"],
                marks={2: "2", 4: "4", 13: "13", 26: "26"},
                tooltip={"placement": "bottom", "always_visible": True}),
                control_id="lab-cot-fast-wrap"),
            _control("Slow MA (weeks)", dcc.Slider(
                id="lab-cot-slow", min=4, max=52, step=1, value=p["cot_slow"],
                marks={4: "4", 16: "16", 26: "26", 52: "52"},
                tooltip={"placement": "bottom", "always_visible": True}),
                control_id="lab-cot-slow-wrap"),
            _control("SI Threshold (% / 100−%)", dcc.Slider(
                id="lab-cot-threshold", min=5, max=45, step=1, value=p["cot_threshold"],
                marks={5: "5", 20: "20", 45: "45"},
                tooltip={"placement": "bottom", "always_visible": True}),
                control_id="lab-cot-threshold-wrap", style={"display": "none"}),
        ],
    )

    return html.Div(
        className="page-container",
        children=[
            html.Div(
                className="page-content",
                children=[
                    dcc.Store(id="lab-store"),
                    html.Div(
                        className="table-panel",
                        children=[
                            html.Div("Strategy Configuration — commodity from topbar selector",
                                     className="panel-heading"),
                            html.Div(
                                className="lab-controls-grid",
                                children=[
                                    _control("Strategy", dcc.RadioItems(
                                        id="lab-strategy", options=_STRATEGY_OPTIONS,
                                        value=p["strategy"], className="lab-radio", inline=True)),
                                    momentum_block, carry_block, statarb_block, cot_block,
                                ],
                            ),
                            html.Div(className="lab-divider"),
                            html.Div(
                                className="lab-controls-grid",
                                children=[
                                    _control("Vol Target (annualized %)", dcc.Slider(
                                        id="lab-vol-target", min=5, max=25, step=1,
                                        value=int(round(p["vol_target"] * 100)),
                                        marks={5: "5%", 15: "15%", 25: "25%"},
                                        tooltip={"placement": "bottom", "always_visible": True})),
                                    _control("Vol Estimation Window (days)", dcc.Slider(
                                        id="lab-vol-window", min=20, max=250, step=10,
                                        value=p["vol_window"],
                                        marks={20: "20", 120: "120", 250: "250"},
                                        tooltip={"placement": "bottom", "always_visible": True})),
                                ],
                            ),
                        ],
                    ),
                    html.Div(id="lab-note", className="placeholder-note", style={"display": "none"}),
                    html.Div(
                        className="metric-row",
                        children=[
                            metric_card("MTM Sharpe", "—", "full sample", id="lab-metric-sharpe"),
                            metric_card("MTM CAGR", "—", "full sample", id="lab-metric-cagr"),
                            metric_card("MTM Max Drawdown", "—", "full sample",
                                        color="#E24B4A", id="lab-metric-drawdown"),
                            metric_card("MTM Total PnL", "—", "full sample, $",
                                        color="#639922", id="lab-metric-pnl"),
                        ],
                    ),
                    # Price Space and MTM Space side by side — the dual view is
                    # methodologically explicit, never show one without the other.
                    html.Div(
                        className="chart-grid-2",
                        children=[
                            html.Div(className="chart-panel", children=[
                                dcc.Graph(id="lab-price-chart", config={"displayModeBar": False},
                                          style={"height": "620px"})]),
                            html.Div(className="chart-panel", children=[
                                dcc.Graph(id="lab-mtm-chart", config={"displayModeBar": False},
                                          style={"height": "620px"})]),
                        ],
                    ),
                    html.Div(
                        id="lab-sweep-panel", className="table-panel",
                        children=[
                            html.Div(
                                className="panel-heading-row",
                                children=[
                                    html.Div("Parameter Sweep — stable Sharpe regions",
                                             className="panel-heading"),
                                    html.Button("Run Sweep", id="lab-sweep-btn",
                                                className="lab-button", n_clicks=0),
                                ],
                            ),
                            dcc.Loading(
                                color="#378ADD",
                                children=dcc.Graph(id="lab-sweep-chart",
                                                   config={"displayModeBar": False},
                                                   style={"height": "440px"}),
                            ),
                        ],
                    ),
                    html.Div(
                        className="table-panel",
                        children=[
                            html.Div("Diagnostics — Full / Pre-Ukraine / Post-Ukraine (MTM)",
                                     className="panel-heading"),
                            dash_table.DataTable(
                                id="lab-diagnostics", data=[], columns=[], **_TABLE_STYLE,
                                style_cell_conditional=[{
                                    "if": {"column_id": "Metric"},
                                    "textAlign": "left", "fontWeight": "600", "minWidth": "140px",
                                }],
                            ),
                        ],
                    ),
                    html.Div(
                        className="table-panel",
                        children=[
                            html.Div("Sample-Split Analytics (Full + Ukraine splits + YoY)",
                                     className="panel-heading"),
                            dash_table.DataTable(
                                id="lab-metrics-table", data=[], columns=[],
                                merge_duplicate_headers=True, **_TABLE_STYLE,
                                style_cell_conditional=[{
                                    "if": {"column_id": "Sample"},
                                    "textAlign": "left", "fontWeight": "600", "minWidth": "170px",
                                }],
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )


# ── control visibility callbacks ──────────────────────────────────────────────

@callback(
    Output("lab-momentum-controls", "style"),
    Output("lab-carry-controls", "style"),
    Output("lab-statarb-controls", "style"),
    Output("lab-cot-controls", "style"),
    Output("lab-sweep-panel", "style"),
    Input("lab-strategy", "value"),
)
def _toggle_strategy_controls(strategy):
    show = {"display": "contents"}   # blocks are transparent members of the controls grid
    hide = {"display": "none"}
    sweep = {} if strategy in ("Momentum", "Stat-Arb") else hide
    return (
        show if strategy == "Momentum" else hide,
        show if strategy == "Carry" else hide,
        show if strategy == "Stat-Arb" else hide,
        show if strategy == "COT" else hide,
        sweep,
    )


@callback(
    Output("lab-mom-fast-wrap", "style"),
    Output("lab-mom-slow-wrap", "style"),
    Input("lab-mom-tier", "value"),
)
def _toggle_momentum_custom(tier):
    style = {"display": "flex"} if tier == "Custom" else {"display": "none"}
    return style, style


@callback(
    Output("lab-cot-fast-wrap", "style"),
    Output("lab-cot-slow-wrap", "style"),
    Output("lab-cot-threshold-wrap", "style"),
    Input("lab-cot-signal", "value"),
)
def _toggle_cot_controls(signal):
    flow = {"display": "flex"} if signal == "Follow the Flow" else {"display": "none"}
    fade = {"display": "flex"} if signal == "Fade the Crowd" else {"display": "none"}
    return flow, flow, fade


@callback(
    Output("lab-sa-exit", "disabled"),
    Input("lab-sa-hedge", "value"),
)
def _toggle_exit_slider(hedge):
    # OLS-β mode exits on mean-cross by construction (rv_regression)
    return hedge == "ols"


@callback(
    Output("lab-carry-front", "options"),
    Output("lab-carry-end", "options"),
    Output("lab-carry-front", "value"),
    Output("lab-carry-end", "value"),
    Input("commodity-store", "data"),
    State("lab-carry-front", "value"),
    State("lab-carry-end", "value"),
)
def _update_carry_options(commodity, cur_front, cur_end):
    commodity = commodity if commodity in lab.available_commodities() else "WTI"
    fcols = lab.available_fcols(commodity)
    opts = [{"label": c, "value": c} for c in fcols]
    front = cur_front if cur_front in fcols else ("F4" if "F4" in fcols else (fcols[0] if fcols else None))
    end = cur_end if cur_end in fcols else ("F15" if "F15" in fcols else (fcols[-1] if fcols else None))
    return opts, opts, front, end


# ── compute callback: params → server-side cache, key → store ─────────────────

@callback(
    Output("lab-store", "data"),
    Output("url", "search"),
    Output("lab-note", "children"),
    Output("lab-note", "style"),
    Input("commodity-store", "data"),
    Input("lab-strategy", "value"),
    Input("lab-mom-tier", "value"),
    Input("lab-mom-fast", "value"),
    Input("lab-mom-slow", "value"),
    Input("lab-carry-front", "value"),
    Input("lab-carry-end", "value"),
    Input("lab-sa-pair", "value"),
    Input("lab-sa-lookback", "value"),
    Input("lab-sa-entry", "value"),
    Input("lab-sa-exit", "value"),
    Input("lab-sa-hedge", "value"),
    Input("lab-sa-tenor", "value"),
    Input("lab-cot-signal", "value"),
    Input("lab-cot-fast", "value"),
    Input("lab-cot-slow", "value"),
    Input("lab-cot-threshold", "value"),
    Input("lab-vol-target", "value"),
    Input("lab-vol-window", "value"),
)
def _compute(commodity, strategy, tier, fast, slow, carry_front, carry_end,
             pair, lookback, entry, exit_thr, hedge, tenor,
             cot_signal, cot_fast, cot_slow, cot_threshold,
             vol_target_pct, vol_window):
    notes = []
    runnable = lab.available_commodities()
    if commodity not in runnable:
        notes.append(f"'{commodity}' has no roll config / lab data — showing WTI instead.")
        commodity = "WTI" if "WTI" in runnable else (runnable[0] if runnable else None)

    if fast is not None and slow is not None and tier == "Custom" and fast >= slow:
        notes.append(f"Fast MA ({fast}) must be < slow MA ({slow}) — slow clamped to {fast + 5}.")
        slow = fast + 5

    raw = dict(
        strategy=strategy, commodity=commodity,
        tier=tier, fast=fast, slow=slow,
        carry_front=carry_front, carry_end=carry_end,
        pair=pair, lookback=lookback, entry=entry, exit=exit_thr,
        hedge=hedge, roll_tenor=tenor,
        cot_signal=cot_signal, cot_fast=cot_fast, cot_slow=cot_slow,
        cot_threshold=cot_threshold,
        vol_target=(vol_target_pct or 15) / 100.0, vol_window=vol_window,
    )

    try:
        key = lab.run_lab(raw)
    except Exception as e:  # surface compute errors instead of a dead page
        msg = f"Compute failed: {type(e).__name__}: {e}"
        return {"error": msg}, no_update, msg, {"display": "block"}

    params = lab.normalize_params(raw)
    if params["strategy"] == "COT" and lab.get_result(key).get("cot_synthetic"):
        notes.append("COT data is SYNTHETIC (cot_bbg table not built yet) — "
                     "signal mechanics are real, PnL is illustrative only.")

    search = "?" + urlencode(params)
    note_text = " ".join(notes)
    note_style = {"display": "block"} if notes else {"display": "none"}
    return {"key": key, "params": params}, search, note_text, note_style


# ── display callbacks: store → outputs ────────────────────────────────────────

@callback(Output("lab-price-chart", "figure"), Input("lab-store", "data"))
def _render_price_space(data):
    if not data or "key" not in data:
        return _placeholder_fig("No result")
    result = lab.get_result(data["key"])
    return spread_figure(result) if result["kind"] == "pair" else price_space_figure(result)


@callback(Output("lab-mtm-chart", "figure"), Input("lab-store", "data"))
def _render_mtm(data):
    if not data or "key" not in data:
        return _placeholder_fig("No result")
    result = lab.get_result(data["key"])
    return mtm_figure(result, vol_target=data["params"].get("vol_target"))


@callback(
    Output("lab-metric-sharpe", "children"),
    Output("lab-metric-cagr", "children"),
    Output("lab-metric-drawdown", "children"),
    Output("lab-metric-pnl", "children"),
    Input("lab-store", "data"),
)
def _render_metric_cards(data):
    if not data or "key" not in data:
        return "—", "—", "—", "—"
    m = lab.get_result(data["key"])["mtm_metrics"]
    return (
        _fmt(m.get("Sharpe"), ".2f"),
        _fmt(m.get("CAGR"), ".1%"),
        _fmt(m.get("Drawdown"), ".1%"),
        f"${m['Total PnL']:,.0f}" if pd.notna(m.get("Total PnL")) else "—",
    )


@callback(
    Output("lab-diagnostics", "data"),
    Output("lab-diagnostics", "columns"),
    Output("lab-metrics-table", "data"),
    Output("lab-metrics-table", "columns"),
    Input("lab-store", "data"),
)
def _render_tables(data):
    if not data or "key" not in data:
        return [], [], [], []
    result = lab.get_result(data["key"])

    diag = lab.diagnostics(result)
    diag_cols = [{"name": c, "id": c} for c in diag.columns]

    split = lab.split_metrics(result)
    split = split.round(3)
    return (
        diag.to_dict("records"), diag_cols,
        split.to_dict("records"), _split_table_columns(split),
    )


@callback(
    Output("lab-sweep-chart", "figure"),
    Input("lab-sweep-btn", "n_clicks"),
    Input("lab-store", "data"),
)
def _render_sweep(n_clicks, data):
    if not data or "key" not in data:
        return _placeholder_fig("No result")
    params = data["params"]
    if params["strategy"] not in ("Momentum", "Stat-Arb"):
        return _placeholder_fig("Sweep available for Momentum and Stat-Arb")

    # Compute only on explicit button click; param changes just redraw the
    # crosshair when a grid for this commodity/pair is already cached.
    explicit = ctx.triggered_id == "lab-sweep-btn"
    if not explicit and not lab.has_cached_sweep(params):
        return _placeholder_fig("Click 'Run Sweep' to compute the Sharpe grid "
                                "(cached per commodity/pair afterwards)")
    pack = lab.sweep_for(params)
    if pack is None:
        return _placeholder_fig("Sweep unavailable")
    grid, info = pack
    return sweep_heatmap(grid, info)

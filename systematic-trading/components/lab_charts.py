"""
components/lab_charts.py — Strategy Lab figure builders.

Every strategy view keeps the Price Space / MTM Space duality explicit:
    price_space_figure / spread_figure  — signal logic in price units
    mtm_figure                          — vol-targeted capital account

All figures share the dark layout from components.charts._LAYOUT_BASE and
mark the Ukraine sub-period boundary (the methodology's structural split).
"""
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from components.charts import _LAYOUT_BASE

_LONG_COLOR = "rgba(99, 153, 34, 0.12)"    # --green, low opacity
_SHORT_COLOR = "rgba(226, 75, 74, 0.12)"   # --red, low opacity
_GREEN = "#639922"
_RED = "#E24B4A"
_BLUE = "#378ADD"
_AMBER = "#EF9F27"
_MUTED = "#9ba3b2"
_FLAT = "#2d3142"

_UKRAINE = pd.Timestamp("2022-02-24")

# Discrete -1/0/+1 colorscale for the position timeline strip.
_POSITION_COLORSCALE = [
    [0.0, _RED], [0.33, _RED],
    [0.34, _FLAT], [0.66, _FLAT],
    [0.67, _GREEN], [1.0, _GREEN],
]


# ── shared helpers ────────────────────────────────────────────────────────────

def _position_blocks(position: pd.Series):
    """Yield (start, end, side) for contiguous nonzero position runs."""
    pos = position.fillna(0)
    groups = (pos != pos.shift()).cumsum()
    for _, block in pos.groupby(groups):
        side = block.iloc[0]
        if side == 0:
            continue
        yield block.index[0], block.index[-1], side


def _add_position_shading(fig: go.Figure, position: pd.Series, row: int) -> None:
    for start, end, side in _position_blocks(position):
        fig.add_vrect(
            x0=start, x1=end,
            fillcolor=_LONG_COLOR if side > 0 else _SHORT_COLOR,
            line_width=0, layer="below", row=row, col=1,
        )


def _add_ukraine_marker(fig: go.Figure, index: pd.DatetimeIndex, n_rows: int) -> None:
    if len(index) == 0 or index.min() > _UKRAINE or index.max() < _UKRAINE:
        return
    for r in range(1, n_rows + 1):
        fig.add_vline(x=_UKRAINE, line=dict(color=_AMBER, width=1, dash="dot"),
                      opacity=0.6, row=r, col=1)
    fig.add_annotation(x=_UKRAINE, yref="paper", y=1.02, text="Ukraine",
                       showarrow=False, font=dict(color=_AMBER, size=9))


def _position_timeline_trace(position: pd.Series) -> go.Heatmap:
    """1-row heatmap strip: green = long, red = short, gray = flat."""
    pos = position.fillna(0.0)
    return go.Heatmap(
        x=pos.index, y=[""], z=[pos.values],
        zmin=-1, zmax=1, colorscale=_POSITION_COLORSCALE,
        showscale=False, hovertemplate="%{x|%Y-%m-%d}: %{z:+.0f}<extra>position</extra>",
    )


def _apply_axes(fig: go.Figure, n_rows: int) -> None:
    for r in range(1, n_rows + 1):
        fig.update_xaxes(**_LAYOUT_BASE["xaxis"], row=r, col=1)
        fig.update_yaxes(**_LAYOUT_BASE["yaxis"], row=r, col=1)
    for ann in fig.layout.annotations:
        if not ann.font or not ann.font.color:
            ann.font = dict(color=_MUTED, size=11)


def _base_layout() -> dict:
    return {k: v for k, v in _LAYOUT_BASE.items() if k not in ("xaxis", "yaxis")}


# ── directional price space ───────────────────────────────────────────────────

def price_space_figure(result: dict) -> go.Figure:
    """
    Price Space for directional strategies (Momentum / Carry / COT), in NATIVE
    quote units: held-contract price with long/short shading, the ±1/0
    position timeline strip, and cumulative $/unit PnL.
    """
    held_price = result["held_price_native"]
    position = result["position"]
    cum_pnl = result["price_space"]["cum_pnl"]

    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06,
        row_heights=[0.55, 0.07, 0.38],
        subplot_titles=("Held Contract Price", "", "Cumulative PnL ($/unit, price space)"),
    )

    _add_position_shading(fig, position, row=1)
    fig.add_trace(
        go.Scatter(x=held_price.index, y=held_price.values, name="Held Price",
                   line=dict(color=_BLUE, width=1.5)),
        row=1, col=1,
    )
    fig.add_trace(_position_timeline_trace(position), row=2, col=1)
    fig.add_trace(
        go.Scatter(x=cum_pnl.index, y=cum_pnl.values, name="Cum PnL",
                   line=dict(color=_AMBER, width=1.5),
                   fill="tozeroy", fillcolor="rgba(239, 159, 39, 0.08)"),
        row=3, col=1,
    )

    _add_ukraine_marker(fig, held_price.index, n_rows=3)
    fig.update_layout(
        **_base_layout(),
        title=dict(text=f"{result['label']} — Price Space (native units)",
                   font=dict(color="#d4dae6", size=14)),
        showlegend=False, height=520,
    )
    _apply_axes(fig, n_rows=3)
    fig.update_yaxes(showticklabels=False, row=2, col=1)
    return fig


# ── stat-arb price space (spread + z-score) ───────────────────────────────────

def spread_figure(result: dict) -> go.Figure:
    """
    Price Space for stat-arb: the spread S(t) with rolling mean ± ε·σ bands,
    the z-score with entry/exit thresholds and position shading, the position
    timeline strip, and cumulative unit-spread PnL — so traders can see
    exactly when and why the signal fires.
    """
    sp = result["spread"]
    position = result["position"]
    entry = result["entry_threshold"]
    exit_thr = result.get("exit_threshold")
    cum_pnl = result["price_space"]["cum_pnl"]

    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.05,
        row_heights=[0.38, 0.28, 0.06, 0.28],
        subplot_titles=("Spread with rolling mean ± ε·σ", "Z-score", "",
                        "Cumulative PnL ($/unit spread)"),
    )

    # Row 1 — spread + bands
    fig.add_trace(go.Scatter(x=sp.index, y=sp["upper_band"], name="+ε·σ",
                             line=dict(color=_RED, width=0.8, dash="dot")), row=1, col=1)
    fig.add_trace(go.Scatter(x=sp.index, y=sp["lower_band"], name="−ε·σ",
                             line=dict(color=_GREEN, width=0.8, dash="dot")), row=1, col=1)
    fig.add_trace(go.Scatter(x=sp.index, y=sp["spread_mean"], name="Mean",
                             line=dict(color=_MUTED, width=1, dash="dash")), row=1, col=1)
    fig.add_trace(go.Scatter(x=sp.index, y=sp["spread"], name="Spread",
                             line=dict(color=_BLUE, width=1.5)), row=1, col=1)

    # Row 2 — z-score with thresholds, shaded by held position
    _add_position_shading(fig, position, row=2)
    fig.add_hline(y=entry, line=dict(color=_RED, width=1, dash="dot"), row=2, col=1)
    fig.add_hline(y=-entry, line=dict(color=_GREEN, width=1, dash="dot"), row=2, col=1)
    if exit_thr is not None and exit_thr > 0:
        fig.add_hline(y=exit_thr, line=dict(color=_MUTED, width=0.8, dash="dot"), row=2, col=1)
        fig.add_hline(y=-exit_thr, line=dict(color=_MUTED, width=0.8, dash="dot"), row=2, col=1)
    fig.add_hline(y=0, line=dict(color=_FLAT, width=1), row=2, col=1)
    fig.add_trace(go.Scatter(x=sp.index, y=sp["zscore"], name="Z",
                             line=dict(color=_AMBER, width=1.2)), row=2, col=1)

    fig.add_trace(_position_timeline_trace(position), row=3, col=1)
    fig.add_trace(
        go.Scatter(x=cum_pnl.index, y=cum_pnl.values, name="Cum PnL",
                   line=dict(color=_AMBER, width=1.5),
                   fill="tozeroy", fillcolor="rgba(239, 159, 39, 0.08)"),
        row=4, col=1,
    )

    _add_ukraine_marker(fig, sp.index, n_rows=4)
    fig.update_layout(
        **_base_layout(),
        title=dict(text=f"{result['label']} — Spread & Signal",
                   font=dict(color="#d4dae6", size=14)),
        showlegend=False, height=620,
    )
    _apply_axes(fig, n_rows=4)
    fig.update_yaxes(showticklabels=False, row=3, col=1)
    return fig


# ── MTM space ─────────────────────────────────────────────────────────────────

def mtm_figure(result: dict, vol_target: float | None = None) -> go.Figure:
    """
    MTM Space: vol-targeted equity index (start = 1.0), drawdown series, and
    the realized-vol scalar applied each day.
    """
    mtm = result["mtm"]
    equity = mtm["equity_index"].astype(float).ffill()
    drawdown = equity / equity.cummax() - 1.0

    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.07,
        row_heights=[0.5, 0.25, 0.25],
        subplot_titles=("Equity Index (vol-targeted, start = 1.0)", "Drawdown", "Vol Scalar"),
    )

    fig.add_trace(
        go.Scatter(x=equity.index, y=equity.values, name="Equity",
                   line=dict(color=_GREEN, width=1.5)),
        row=1, col=1,
    )
    fig.add_hline(y=1.0, line=dict(color=_FLAT, width=1), row=1, col=1)
    fig.add_trace(
        go.Scatter(x=drawdown.index, y=drawdown.values, name="Drawdown",
                   line=dict(color=_RED, width=1.2),
                   fill="tozeroy", fillcolor="rgba(226, 75, 74, 0.12)"),
        row=2, col=1,
    )
    if "vol_scalar" in mtm.columns:
        fig.add_trace(
            go.Scatter(x=mtm.index, y=mtm["vol_scalar"], name="Vol Scalar",
                       line=dict(color=_BLUE, width=1.2)),
            row=3, col=1,
        )

    _add_ukraine_marker(fig, mtm.index, n_rows=3)
    vt_text = f" — {vol_target:.0%} vol target" if vol_target is not None else ""
    fig.update_layout(
        **_base_layout(),
        title=dict(text=f"{result['label']} — MTM Space{vt_text}",
                   font=dict(color="#d4dae6", size=14)),
        showlegend=False, height=520,
    )
    _apply_axes(fig, n_rows=3)
    fig.update_yaxes(tickformat=".0%", row=2, col=1)
    return fig


# ── parameter sweep heatmap ───────────────────────────────────────────────────

def sweep_heatmap(grid: pd.DataFrame, info: dict) -> go.Figure:
    """
    Sharpe over a 2D parameter grid as a plotly heatmap, with the current
    parameter selection highlighted as a crosshair. Categorical axes (the
    grids are irregular), diverging red→gray→green scale centered at 0 so
    stable Sharpe REGIONS — not point estimates — stand out.
    """
    x_labels = [str(c) for c in grid.columns]
    y_labels = [str(i) for i in grid.index]
    z = grid.values.astype(float)

    abs_max = max(abs(pd.DataFrame(z).min().min() or 0),
                  abs(pd.DataFrame(z).max().max() or 0), 0.1)

    fig = go.Figure(
        go.Heatmap(
            x=x_labels, y=y_labels, z=z,
            zmin=-abs_max, zmax=abs_max,
            colorscale=[[0.0, _RED], [0.5, "#1e2235"], [1.0, _GREEN]],
            colorbar=dict(title=dict(text="Sharpe", font=dict(color=_MUTED, size=10)),
                          tickfont=dict(color=_MUTED, size=9), thickness=10),
            hovertemplate=(f"{info['y_title']}: %{{y}}<br>{info['x_title']}: %{{x}}"
                           "<br>Sharpe: %{z:.2f}<extra></extra>"),
        )
    )

    # Crosshair on the nearest grid cell to the current parameter selection
    def _nearest(values, target) -> str | None:
        try:
            vals = [float(v) for v in values]
            return str(values[min(range(len(vals)), key=lambda i: abs(vals[i] - float(target)))])
        except (TypeError, ValueError):
            return None

    cx = _nearest(list(grid.columns), info.get("cur_x"))
    cy = _nearest(list(grid.index), info.get("cur_y"))
    if cx is not None:
        fig.add_vline(x=cx, line=dict(color=_AMBER, width=1, dash="dash"), opacity=0.9)
    if cy is not None:
        fig.add_hline(y=cy, line=dict(color=_AMBER, width=1, dash="dash"), opacity=0.9)

    base = _base_layout()
    fig.update_layout(
        **base,
        title=dict(text=info.get("title", "Parameter Sweep"), font=dict(color="#d4dae6", size=14)),
        xaxis=dict(title=dict(text=info["x_title"], font=dict(color=_MUTED, size=11)),
                   type="category", tickfont=dict(color="#6b7280"), showgrid=False),
        yaxis=dict(title=dict(text=info["y_title"], font=dict(color=_MUTED, size=11)),
                   type="category", tickfont=dict(color="#6b7280"), showgrid=False),
        height=420,
    )
    return fig

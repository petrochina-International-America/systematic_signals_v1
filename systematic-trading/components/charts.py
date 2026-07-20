"""
components/charts.py — shared figure builders (dark theme base + COT views).

_LAYOUT_BASE is the single source of the dark plotly theme; lab_charts.py and
every page import it from here.
"""
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd

_LAYOUT_BASE = dict(
    paper_bgcolor="#12151f",
    plot_bgcolor="#12151f",
    font=dict(family="Inter, system-ui, sans-serif", color="#9ba3b2", size=12),
    margin=dict(l=50, r=30, t=40, b=40),
    xaxis=dict(
        showgrid=True,
        gridcolor="#1e2235",
        zeroline=False,
        tickfont=dict(color="#6b7280"),
    ),
    yaxis=dict(
        showgrid=True,
        gridcolor="#1e2235",
        zeroline=False,
        tickfont=dict(color="#6b7280"),
    ),
    legend=dict(
        bgcolor="rgba(0,0,0,0)",
        bordercolor="rgba(0,0,0,0)",
        font=dict(color="#9ba3b2"),
    ),
    hovermode="x unified",
)

_GREEN = "#639922"
_RED = "#E24B4A"
_BLUE = "#378ADD"
_AMBER = "#EF9F27"
_MUTED = "#9ba3b2"


def cot_chart(
    cot_df: pd.DataFrame,
    price_df: pd.DataFrame,
    commodity: str,
    ma_df: pd.DataFrame | None = None,
) -> go.Figure:
    """
    MM positioning vs price: net-position bars (green/red by sign) with the
    front-month price on a secondary axis. When ma_df (output of
    data.cot.follow_the_flow) is provided, the fast/slow MA crossover lines
    are overlaid on the positioning axis.
    """
    fig = make_subplots(specs=[[{"secondary_y": True}]])

    colors = [_GREEN if v >= 0 else _RED for v in cot_df["mm_net"]]
    fig.add_trace(
        go.Bar(x=cot_df["date"], y=cot_df["mm_net"], name="MM Net",
               marker_color=colors, opacity=0.6),
        secondary_y=False,
    )

    if ma_df is not None:
        fig.add_trace(
            go.Scatter(x=ma_df.index, y=ma_df["ma_fast"], name="MA fast",
                       line=dict(color=_BLUE, width=1.4)),
            secondary_y=False,
        )
        fig.add_trace(
            go.Scatter(x=ma_df.index, y=ma_df["ma_slow"], name="MA slow",
                       line=dict(color=_MUTED, width=1.4, dash="dash")),
            secondary_y=False,
        )

    if not price_df.empty:
        fig.add_trace(
            go.Scatter(x=price_df["date"], y=price_df["close"],
                       name=f"{commodity} Price",
                       line=dict(color=_AMBER, width=1.5)),
            secondary_y=True,
        )

    base = {k: v for k, v in _LAYOUT_BASE.items() if k != "yaxis"}
    fig.update_layout(
        **base,
        title=dict(text=f"{commodity} — MM Positioning vs Price",
                   font=dict(color="#d4dae6", size=14)),
        yaxis=dict(**_LAYOUT_BASE["yaxis"], title="MM Net Contracts"),
        yaxis2=dict(showgrid=False, tickfont=dict(color="#6b7280"),
                    title="Price", overlaying="y", side="right"),
    )
    return fig


def sentiment_figure(si_df: pd.DataFrame, threshold_pct: float, commodity: str) -> go.Figure:
    """
    Sentiment Index timeseries (0–100) with the buy/sell threshold bands for
    the Fade the Crowd signal. si_df is the output of data.cot.fade_the_crowd.
    """
    fig = go.Figure()

    fig.add_hrect(y0=0, y1=threshold_pct, fillcolor="rgba(99, 153, 34, 0.10)",
                  line_width=0, layer="below")
    fig.add_hrect(y0=100 - threshold_pct, y1=100, fillcolor="rgba(226, 75, 74, 0.10)",
                  line_width=0, layer="below")
    fig.add_hline(y=threshold_pct, line=dict(color=_GREEN, width=1, dash="dot"),
                  annotation_text=f"buy < {threshold_pct:.0f}",
                  annotation_font=dict(color=_GREEN, size=10))
    fig.add_hline(y=100 - threshold_pct, line=dict(color=_RED, width=1, dash="dot"),
                  annotation_text=f"sell > {100 - threshold_pct:.0f}",
                  annotation_font=dict(color=_RED, size=10))

    fig.add_trace(
        go.Scatter(x=si_df.index, y=si_df["sentiment_index"], name="Sentiment Index",
                   line=dict(color=_BLUE, width=1.5)),
    )

    base = {k: v for k, v in _LAYOUT_BASE.items() if k != "yaxis"}
    fig.update_layout(
        **base,
        title=dict(text=f"{commodity} — Sentiment Index (Fade the Crowd)",
                   font=dict(color="#d4dae6", size=14)),
        yaxis=dict(**_LAYOUT_BASE["yaxis"], title="SI (0–100)", range=[-5, 105]),
        showlegend=False,
    )
    return fig


def percentile_histogram(cot_df: pd.DataFrame, commodity: str) -> go.Figure:
    """
    Distribution of the rolling 52-week percentile rank of MM net positioning,
    with the latest reading marked — shows how unusual current crowding is.
    """
    ranks = cot_df["percentile_rank"].dropna()
    latest = float(ranks.iloc[-1]) if len(ranks) else None

    fig = go.Figure(
        go.Histogram(x=ranks, nbinsx=20, marker_color=_BLUE, opacity=0.7,
                     name="52w percentile rank")
    )
    if latest is not None:
        fig.add_vline(x=latest, line=dict(color=_AMBER, width=2),
                      annotation_text=f"now: {latest:.0f}th",
                      annotation_font=dict(color=_AMBER, size=10))

    base = {k: v for k, v in _LAYOUT_BASE.items() if k not in ("xaxis", "yaxis", "hovermode")}
    fig.update_layout(
        **base,
        title=dict(text=f"{commodity} — Positioning Percentile Distribution",
                   font=dict(color="#d4dae6", size=14)),
        xaxis=dict(**_LAYOUT_BASE["xaxis"], title="52-week percentile rank"),
        yaxis=dict(**_LAYOUT_BASE["yaxis"], title="Weeks"),
        showlegend=False,
        bargap=0.05,
    )
    return fig

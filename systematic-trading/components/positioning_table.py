from dash import dash_table
import pandas as pd


_CROWDING_COLORS = {
    "Crowded": "#E24B4A",
    "Neutral": "#378ADD",
    "Washed": "#639922",
}


def positioning_table(df: pd.DataFrame) -> dash_table.DataTable:
    """
    Render a styled dark-theme DataTable for the COT positioning snapshot.

    Expects columns: commodity, mm_net, percentile_rank, crowding_flag.
    """
    display = df[["commodity", "mm_net", "percentile_rank", "crowding_flag"]].copy()
    display["mm_net"] = display["mm_net"].apply(lambda v: f"{v:,}")
    display["percentile_rank"] = display["percentile_rank"].apply(lambda v: f"{v:.1f}th")
    display.columns = ["Commodity", "MM Net", "Pct Rank", "Crowding"]

    style_data_conditional = []
    for flag, color in _CROWDING_COLORS.items():
        style_data_conditional.append(
            {
                "if": {"filter_query": f'{{Crowding}} = "{flag}"', "column_id": "Crowding"},
                "color": color,
                "fontWeight": "600",
            }
        )

    return dash_table.DataTable(
        data=display.to_dict("records"),
        columns=[{"name": c, "id": c} for c in display.columns],
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

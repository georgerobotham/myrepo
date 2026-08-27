"""
Hydraulic Data Explorer
-----------------------
Generic multi-parameter viewer for wide-format hydraulic CSV files.
Launch this script; a startup window lets you configure the row structure
and filters before loading. The Dash viewer then opens in your browser.
"""

import sys
import threading
import webbrowser
import time
import re
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter import ttk

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.cm as cm
import numpy as np
import dash
from dash import dcc, html, dash_table, Input, Output, State, ctx

# =========================================================
# CONSTANTS
# =========================================================
PORT = 8050
ACCENT  = "#1B4F72"
BG      = "#F8F9FA"
BORDER  = "#DEE2E6"
MUTED   = "#6C757D"
WHITE   = "#FFFFFF"
GREEN   = "#28A745"
RED     = "#DC3545"

# =========================================================
# HELPERS
# =========================================================
def clean(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def norm_scenario(value) -> str:
    return clean(value).upper().replace(" ", "_").replace("-", "_")


def norm_aep(value) -> str:
    return clean(value).lower().replace("%", "p").replace(" ", "")


def sanitise_col(name: str) -> str:
    """Make a parameter name safe for use as a column header."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name.strip()).strip("_") or "Value"


def parse_filter_list(text: str) -> set[str]:
    """Turn a comma-separated string into a set of stripped values. Empty = no filter."""
    if not text.strip():
        return set()
    return {v.strip() for v in text.split(",") if v.strip()}


# =========================================================
# DATA LOADING
# =========================================================
def load_csv(path: Path, cfg: dict, log) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Parse a wide-format hydraulic CSV using the row-mapping in cfg.

    cfg keys:
        row_param, row_location, row_model, row_scenario, row_climate, row_aep
        row_data_start, col_time
        filter_aeps      : set[str] — empty means include all
        filter_scenarios : set[str] — empty means include all
        filter_locations : set[str] — empty means include all
    """
    log(f"Reading file…  ({path.name})")
    df_raw = pd.read_csv(path, header=None, low_memory=False)
    log(f"Raw shape: {df_raw.shape[0]:,} rows × {df_raw.shape[1]:,} columns")

    r_param    = cfg["row_param"]
    r_loc      = cfg["row_location"]
    r_model    = cfg["row_model"]     # may be None
    r_scenario = cfg["row_scenario"]  # may be None
    r_climate  = cfg["row_climate"]   # may be None
    r_aep      = cfg["row_aep"]       # may be None
    r_data     = cfg["row_data_start"]
    c_time     = cfg["col_time"]

    filter_aeps      = cfg["filter_aeps"]
    filter_scenarios = cfg["filter_scenarios"]
    filter_locations = cfg["filter_locations"]

    def read_row(row_idx, col_idx, normaliser=clean):
        if row_idx is None:
            return ""
        return normaliser(df_raw.iloc[row_idx, col_idx])

    data = df_raw.iloc[r_data:].reset_index(drop=True)
    raw_time = data.iloc[:, c_time]
    time_unit = detect_time_unit(raw_time)
    log(f"Time column auto-detected as: {time_unit}")
    time_col = apply_time_unit(raw_time, time_unit)

    optional = {k: v for k, v in [("row_model", r_model), ("row_scenario", r_scenario),
                                    ("row_climate", r_climate), ("row_aep", r_aep)] if v is None}
    if optional:
        log(f"Fields not mapped (will be blank): {', '.join(optional.keys())}")

    log("Building long-format table…")
    records = []

    for i in range(df_raw.shape[1]):
        if i == c_time:
            continue

        param    = read_row(r_param, i)
        loc      = read_row(r_loc, i)
        model    = read_row(r_model, i)
        scenario = read_row(r_scenario, i, norm_scenario)
        climate  = read_row(r_climate, i)
        aep      = read_row(r_aep, i, norm_aep)

        if not param or not loc:
            continue
        if filter_aeps and r_aep is not None and aep not in filter_aeps:
            continue
        if filter_scenarios and r_scenario is not None and scenario not in filter_scenarios:
            continue
        if filter_locations and loc not in filter_locations:
            continue

        values = pd.to_numeric(data.iloc[:, i], errors="coerce")
        tmp = pd.DataFrame({
            "Time_min": time_col,
            "Value":    values,
            "Parameter": param,
            "Location":  loc,
            "Scenario":  scenario,
            "AEP":       aep,
            "Climate":   climate,
            "Model":     model,
        }).dropna(subset=["Time_min", "Value"])

        if not tmp.empty:
            records.append(tmp)

    if not records:
        raise ValueError(
            "No data found with the current row mapping and filters.\n"
            "Check the row assignments and filter settings."
        )

    long_df = pd.concat(records, ignore_index=True)
    long_df["Time_min"] = long_df["Time_min"].round(2)
    long_df["Value"]    = long_df["Value"].round(4)

    n_params = long_df["Parameter"].nunique()
    n_locs   = long_df["Location"].nunique()
    log(
        f"Loaded: {len(long_df):,} rows · {n_locs} locations · "
        f"{n_params} parameter(s): {', '.join(sorted(long_df['Parameter'].unique()))}"
    )

    log("Building summary table…")
    summary_df = make_summary(long_df)
    log("Done.")
    return long_df, summary_df


def make_summary(df: pd.DataFrame) -> pd.DataFrame:
    summary = (
        df.groupby(["Parameter", "Location", "Scenario", "AEP", "Climate"])
        .agg(
            Peak_value=("Value", "max"),
            Mean_value=("Value", "mean"),
            N_timesteps=("Value", "count"),
        )
        .reset_index()
    )

    peak_times = (
        df.sort_values(["Parameter", "Location", "Scenario", "AEP", "Climate", "Value", "Time_min"])
        .groupby(["Parameter", "Location", "Scenario", "AEP", "Climate"], as_index=False)
        .tail(1)[["Parameter", "Location", "Scenario", "AEP", "Climate", "Time_min"]]
        .rename(columns={"Time_min": "Time_of_peak_min"})
    )

    return summary.merge(
        peak_times,
        on=["Parameter", "Location", "Scenario", "AEP", "Climate"],
        how="left"
    ).round(4)


# =========================================================
# HYDROGRAPH PLOTTING
# =========================================================
# Colour palette for overlaid series
_SERIES_COLOURS = [
    "#1B4F72", "#E74C3C", "#27AE60", "#F39C12", "#8E44AD",
    "#2980B9", "#C0392B", "#16A085", "#D35400", "#6C3483",
    "#117A65", "#CB4335", "#1A5276", "#7D6608", "#4A235A",
]

def build_hydrograph(df: pd.DataFrame, location: str, parameter: str,
                     scenarios: list, aeps: list) -> plt.Figure:
    """Return a matplotlib Figure of overlaid hydrograph series."""
    fig, ax = plt.subplots(figsize=(12, 6))
    fig.patch.set_facecolor("white")

    sub = df[
        (df["Location"] == location) &
        (df["Parameter"] == parameter)
    ]

    colour_idx = 0
    plotted = 0
    for scenario in scenarios:
        for aep in aeps:
            series = sub[
                (sub["Scenario"] == scenario) &
                (sub["AEP"] == aep)
            ].sort_values("Time_min")
            if series.empty:
                continue
            colour = _SERIES_COLOURS[colour_idx % len(_SERIES_COLOURS)]
            label = f"{scenario}  |  {aep}"
            ax.plot(series["Time_min"], series["Value"],
                    label=label, color=colour, linewidth=1.8)
            colour_idx += 1
            plotted += 1

    ax.set_title(f"{location} — {parameter}", fontsize=14, fontweight="bold",
                 color=ACCENT, pad=12)
    ax.set_xlabel("Time (minutes)", fontsize=11)
    ax.set_ylabel(parameter, fontsize=11)
    ax.grid(True, linestyle="--", alpha=0.4)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    if plotted > 0:
        ax.legend(fontsize=9, framealpha=0.8, loc="best")
    else:
        ax.text(0.5, 0.5, "No data for this combination",
                transform=ax.transAxes, ha="center", va="center",
                fontsize=12, color="grey")

    fig.tight_layout()
    return fig


# =========================================================
# DASH APP
# =========================================================
def build_dash_app(long_df: pd.DataFrame, summary_df: pd.DataFrame) -> dash.Dash:
    all_params    = sorted(long_df["Parameter"].dropna().unique())
    all_locations = sorted(long_df["Location"].dropna().unique())
    all_scenarios = sorted(long_df["Scenario"].dropna().unique())
    all_aeps      = sorted(long_df["AEP"].dropna().unique())
    all_climates  = sorted(long_df["Climate"].dropna().unique())

    app = dash.Dash(__name__, title="Hydraulic Data Explorer")

    label_style = {
        "fontSize": "11px", "fontWeight": "600", "color": MUTED,
        "textTransform": "uppercase", "letterSpacing": "0.05em",
        "marginBottom": "4px", "display": "block",
    }
    dd = {"fontSize": "13px", "marginBottom": "16px"}

    tab_style          = {"fontSize": "13px", "padding": "8px 18px"}
    tab_selected_style = {"fontSize": "13px", "fontWeight": "700",
                          "padding": "8px 18px", "borderTop": f"3px solid {ACCENT}"}

    # ── Shared filter bar ─────────────────────────────────────────────
    filter_bar = html.Div([
        html.Div([
            html.Label("Parameter", style=label_style),
            dcc.Dropdown(id="filter-param",
                         options=[{"label": p, "value": p} for p in all_params],
                         value=all_params[0], clearable=False, style=dd),
        ], style={"flex": "1", "marginRight": "16px"}),
        html.Div([
            html.Label("Locations", style=label_style),
            dcc.Dropdown(id="filter-location",
                         options=[{"label": l, "value": l} for l in all_locations],
                         multi=True, placeholder="All locations", style=dd),
        ], style={"flex": "2", "marginRight": "16px"}),
        html.Div([
            html.Label("Scenario", style=label_style),
            dcc.Dropdown(id="filter-scenario",
                         options=[{"label": s, "value": s} for s in all_scenarios],
                         multi=True, placeholder="All scenarios", style=dd),
        ], style={"flex": "1", "marginRight": "16px"}),
        html.Div([
            html.Label("AEP", style=label_style),
            dcc.Dropdown(id="filter-aep",
                         options=[{"label": a, "value": a} for a in all_aeps],
                         multi=True, placeholder="All AEPs", style=dd),
        ], style={"flex": "1", "marginRight": "16px"}),
        html.Div([
            html.Label("Climate", style=label_style),
            dcc.Dropdown(id="filter-climate",
                         options=[{"label": c, "value": c} for c in all_climates],
                         multi=True, placeholder="All climates", style=dd),
        ], style={"flex": "1"}),
    ], style={"display": "flex", "background": WHITE,
              "border": f"1px solid {BORDER}", "borderRadius": "8px",
              "padding": "16px", "marginBottom": "16px"})

    # ── Hydrograph export tab layout ──────────────────────────────────
    hydro_tab_content = html.Div([
        html.P("Build your chart: choose a parameter, which locations, which scenarios "
               "and AEPs to overlay, then preview or export PNGs.",
               style={"fontSize": "13px", "color": MUTED, "marginBottom": "16px"}),

        # Controls row
        html.Div([
            # Parameter
            html.Div([
                html.Label("Parameter", style=label_style),
                dcc.Dropdown(id="hg-param",
                             options=[{"label": p, "value": p} for p in all_params],
                             value=all_params[0], clearable=False),
            ], style={"flex": "1", "marginRight": "16px"}),

            # Locations (multi)
            html.Div([
                html.Label("Locations to export", style=label_style),
                dcc.Dropdown(id="hg-locations",
                             options=[{"label": l, "value": l} for l in all_locations],
                             multi=True, placeholder="Pick locations…"),
            ], style={"flex": "2", "marginRight": "16px"}),
        ], style={"display": "flex", "marginBottom": "12px"}),

        # Scenarios + AEPs checkboxes
        html.Div([
            html.Div([
                html.Label("Scenarios to include", style=label_style),
                dcc.Checklist(
                    id="hg-scenarios",
                    options=[{"label": f"  {s}", "value": s} for s in all_scenarios],
                    value=all_scenarios,
                    inputStyle={"marginRight": "6px"},
                    labelStyle={"display": "block", "fontSize": "13px",
                                "marginBottom": "4px"},
                ),
            ], style={"flex": "1", "marginRight": "32px"}),

            html.Div([
                html.Label("AEPs to include", style=label_style),
                dcc.Checklist(
                    id="hg-aeps",
                    options=[{"label": f"  {a}", "value": a} for a in all_aeps],
                    value=all_aeps,
                    inputStyle={"marginRight": "6px"},
                    labelStyle={"display": "block", "fontSize": "13px",
                                "marginBottom": "4px"},
                ),
            ], style={"flex": "1", "marginRight": "32px"}),

            # Output folder + buttons
            html.Div([
                html.Label("Output folder (paste full path)", style=label_style),
                dcc.Input(id="hg-folder", type="text",
                          placeholder="e.g.  C:\\Reports\\Hydrographs",
                          style={"width": "100%", "fontSize": "13px",
                                 "padding": "6px", "border": f"1px solid {BORDER}",
                                 "borderRadius": "4px", "marginBottom": "10px"}),
                html.Button("Preview chart (first location)",
                            id="hg-preview-btn",
                            style={"width": "100%", "marginBottom": "8px",
                                   "padding": "8px", "fontSize": "13px",
                                   "background": WHITE, "color": ACCENT,
                                   "border": f"2px solid {ACCENT}",
                                   "borderRadius": "4px", "cursor": "hand2",
                                   "fontWeight": "600"}),
                html.Button("Export PNGs for all selected locations",
                            id="hg-export-btn",
                            style={"width": "100%", "padding": "8px",
                                   "fontSize": "13px", "background": ACCENT,
                                   "color": WHITE, "border": "none",
                                   "borderRadius": "4px", "cursor": "hand2",
                                   "fontWeight": "600"}),
            ], style={"flex": "1.5"}),
        ], style={"display": "flex", "background": WHITE,
                  "border": f"1px solid {BORDER}", "borderRadius": "8px",
                  "padding": "16px", "marginBottom": "16px"}),

        # Status message
        html.Div(id="hg-status",
                 style={"fontSize": "13px", "color": MUTED, "marginBottom": "12px"}),

        # Preview image
        html.Div(id="hg-preview-container"),
    ], style={"padding": "8px 0"})

    # ── Full layout ───────────────────────────────────────────────────
    app.layout = html.Div(
        style={"fontFamily": "Segoe UI, Arial, sans-serif", "background": BG,
               "minHeight": "100vh", "padding": "24px"},
        children=[
            html.Div([
                html.H1("Hydraulic Data Explorer",
                        style={"fontSize": "22px", "fontWeight": "700",
                               "color": ACCENT, "margin": "0 0 4px"}),
                html.P(
                    f"{long_df['Location'].nunique()} locations · "
                    f"{long_df['Scenario'].nunique()} scenarios · "
                    f"{long_df['AEP'].nunique()} AEPs · "
                    f"{len(all_params)} parameter(s): {', '.join(all_params)} · "
                    f"{len(long_df):,} total rows",
                    style={"fontSize": "13px", "color": MUTED, "margin": 0}
                ),
            ], style={"marginBottom": "20px"}),

            filter_bar,

            dcc.Tabs(id="tabs", value="summary", children=[
                dcc.Tab(label="Summary",            value="summary",
                        style=tab_style, selected_style=tab_selected_style),
                dcc.Tab(label="Full timeseries",    value="timeseries",
                        style=tab_style, selected_style=tab_selected_style),
                dcc.Tab(label="Export Hydrographs", value="hydrograph",
                        style=tab_style, selected_style=tab_selected_style),
            ], style={"marginBottom": "12px"}),

            # Row count + CSV export (hidden on hydrograph tab)
            html.Div([
                html.Span(id="row-count",
                          style={"fontSize": "13px", "color": MUTED}),
                html.Button("Export filtered CSV", id="export-btn", style={
                    "fontSize": "12px", "marginLeft": "16px", "padding": "4px 12px",
                    "cursor": "pointer", "background": ACCENT, "color": WHITE,
                    "border": "none", "borderRadius": "4px",
                }),
                dcc.Download(id="download"),
            ], id="csv-export-row",
               style={"marginBottom": "8px", "display": "flex", "alignItems": "center"}),

            html.Div(id="table-container"),
            hydro_tab_content,
        ],
    )

    # ── Helpers ───────────────────────────────────────────────────────
    def apply_filters(param, location, scenario, aep, climate, source_df):
        f = source_df.copy()
        if param and "Parameter" in f.columns:
            f = f[f["Parameter"] == param]
        if location: f = f[f["Location"].isin(location)]
        if scenario: f = f[f["Scenario"].isin(scenario)]
        if aep:      f = f[f["AEP"].isin(aep)]
        if climate:  f = f[f["Climate"].isin(climate)]
        return f

    def make_table(df, max_rows=2000):
        display = df.head(max_rows)
        return dash_table.DataTable(
            data=display.to_dict("records"),
            columns=[{"name": c, "id": c} for c in display.columns],
            page_size=25, sort_action="native", filter_action="native",
            style_table={"overflowX": "auto"},
            style_header={"backgroundColor": ACCENT, "color": WHITE,
                          "fontWeight": "600", "fontSize": "12px", "border": "none"},
            style_cell={"fontSize": "12px", "padding": "6px 10px",
                        "fontFamily": "Segoe UI, Arial, sans-serif",
                        "border": f"1px solid {BORDER}"},
            style_data_conditional=[{"if": {"row_index": "odd"}, "backgroundColor": BG}],
        )

    SUMMARY_COLS    = ["Parameter", "Location", "Scenario", "AEP", "Climate",
                       "Peak_value", "Mean_value", "Time_of_peak_min", "N_timesteps"]
    TIMESERIES_COLS = ["Time_min", "Value", "Parameter", "Location",
                       "Scenario", "AEP", "Climate", "Model"]

    # ── Callbacks: table + CSV export ────────────────────────────────
    @app.callback(
        Output("table-container", "children"),
        Output("row-count", "children"),
        Output("csv-export-row", "style"),
        Output("hg-preview-container", "style"),  # show/hide hydro panel
        Input("tabs", "value"),
        Input("filter-param", "value"),
        Input("filter-location", "value"),
        Input("filter-scenario", "value"),
        Input("filter-aep", "value"),
        Input("filter-climate", "value"),
    )
    def update_table(tab, param, location, scenario, aep, climate):
        hide = {"display": "none"}
        show = {}
        csv_row_style  = {"marginBottom": "8px", "display": "flex", "alignItems": "center"}

        if tab == "hydrograph":
            return None, "", hide, show

        if tab == "summary":
            filtered = apply_filters(param, location, scenario, aep, climate, summary_df)
            filtered = filtered[[c for c in SUMMARY_COLS if c in filtered.columns]]
            label = f"{len(filtered):,} rows in summary"
        else:
            filtered = apply_filters(param, location, scenario, aep, climate, long_df)
            filtered = filtered[[c for c in TIMESERIES_COLS if c in filtered.columns]]
            label = f"{len(filtered):,} rows (showing first 2,000)"

        return make_table(filtered), label, csv_row_style, hide

    @app.callback(
        Output("download", "data"),
        Input("export-btn", "n_clicks"),
        State("tabs", "value"),
        State("filter-param", "value"),
        State("filter-location", "value"),
        State("filter-scenario", "value"),
        State("filter-aep", "value"),
        State("filter-climate", "value"),
        prevent_initial_call=True,
    )
    def export_csv(n_clicks, tab, param, location, scenario, aep, climate):
        slug = sanitise_col(param).lower()
        if tab == "summary":
            filtered = apply_filters(param, location, scenario, aep, climate, summary_df)
            filtered = filtered[[c for c in SUMMARY_COLS if c in filtered.columns]]
            filename = f"{slug}_summary_filtered.csv"
        else:
            filtered = apply_filters(param, location, scenario, aep, climate, long_df)
            filtered = filtered[[c for c in TIMESERIES_COLS if c in filtered.columns]]
            filename = f"{slug}_timeseries_filtered.csv"
        return dcc.send_data_frame(filtered.to_csv, filename, index=False)

    # ── Callbacks: hydrograph preview + export ────────────────────────
    @app.callback(
        Output("hg-preview-container", "children"),
        Output("hg-status", "children"),
        Output("hg-status", "style"),
        Input("hg-preview-btn", "n_clicks"),
        Input("hg-export-btn", "n_clicks"),
        State("hg-param", "value"),
        State("hg-locations", "value"),
        State("hg-scenarios", "value"),
        State("hg-aeps", "value"),
        State("hg-folder", "value"),
        prevent_initial_call=True,
    )
    def handle_hydrograph(preview_clicks, export_clicks,
                          param, locations, scenarios, aeps, folder):
        import base64, io as _io

        triggered = ctx.triggered_id
        ok_style   = {"fontSize": "13px", "color": GREEN,   "marginBottom": "12px"}
        err_style  = {"fontSize": "13px", "color": RED,     "marginBottom": "12px"}
        info_style = {"fontSize": "13px", "color": MUTED,   "marginBottom": "12px"}

        if not param:
            return None, "Select a parameter first.", info_style
        if not locations:
            return None, "Select at least one location.", info_style
        if not scenarios or not aeps:
            return None, "Select at least one scenario and one AEP.", info_style

        # ── PREVIEW ──────────────────────────────────────────────────
        if triggered == "hg-preview-btn":
            fig = build_hydrograph(long_df, locations[0], param, scenarios, aeps)
            buf = _io.BytesIO()
            fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
            plt.close(fig)
            buf.seek(0)
            encoded = base64.b64encode(buf.read()).decode()
            img = html.Img(src=f"data:image/png;base64,{encoded}",
                           style={"maxWidth": "100%", "border": f"1px solid {BORDER}",
                                  "borderRadius": "6px"})
            return img, f"Preview: {locations[0]} — {param}", info_style

        # ── EXPORT ───────────────────────────────────────────────────
        if triggered == "hg-export-btn":
            if not folder or not folder.strip():
                return None, "Please enter an output folder path.", err_style

            out_dir = Path(folder.strip())
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return None, f"Cannot create folder: {e}", err_style

            saved = []
            errors = []
            for loc in locations:
                try:
                    fig = build_hydrograph(long_df, loc, param, scenarios, aeps)
                    safe_loc  = re.sub(r'[<>:"/\\|?*]', "_", loc)
                    safe_param = re.sub(r'[<>:"/\\|?*]', "_", param)
                    fname = out_dir / f"{safe_loc}_{safe_param}.png"
                    fig.savefig(fname, dpi=150, bbox_inches="tight")
                    plt.close(fig)
                    saved.append(fname.name)
                except Exception as e:
                    errors.append(f"{loc}: {e}")

            if errors:
                msg = f"Exported {len(saved)} PNG(s) with {len(errors)} error(s): {'; '.join(errors)}"
                return None, msg, err_style

            msg = f"✓ Exported {len(saved)} PNG(s) to:  {out_dir}"
            return None, msg, ok_style

        return None, "", info_style

    return app


# =========================================================
# TIME AUTO-DETECTION
# =========================================================
def detect_time_unit(series: pd.Series) -> str:
    """
    Return 'hours', 'minutes', or 'datetime' based on the time column values.
    'datetime'  → pandas can parse them as timestamps
    'hours'     → numeric, max ≤ 8784 (one year in hours) but looks small (≤ 1000)
    'minutes'   → everything else numeric
    """
    sample = series.dropna().head(50)

    # Try datetime parse
    try:
        pd.to_datetime(sample, infer_datetime_format=True)
        return "datetime"
    except Exception:
        pass

    numeric = pd.to_numeric(sample, errors="coerce").dropna()
    if numeric.empty:
        return "minutes"

    mx = numeric.max()
    if mx <= 500:
        return "hours"
    return "minutes"


def apply_time_unit(series: pd.Series, unit: str) -> pd.Series:
    if unit == "datetime":
        dt = pd.to_datetime(series, infer_datetime_format=True, errors="coerce")
        origin = dt.dropna().iloc[0]
        return ((dt - origin).dt.total_seconds() / 60).round(2)
    numeric = pd.to_numeric(series, errors="coerce")
    if unit == "hours":
        return (numeric * 60).round(2)
    return numeric.round(2)


# =========================================================
# LAUNCHER WINDOW
# =========================================================
FIELD_DEFS = [
    # (display_label,   cfg_key,        required)
    ("Parameter/Type",  "row_param",    True),
    ("Location",        "row_location", True),
    ("Time column",     "col_time",     True),
    ("Data starts at row", "row_data_start", True),
    ("Model",           "row_model",    False),
    ("Scenario",        "row_scenario", False),
    ("AEP",             "row_aep",      False),
    ("Climate",         "row_climate",  False),
]

NOT_PRESENT = "— not in this file —"
PREVIEW_ROWS = 10   # how many rows of the CSV to show in the preview


class LauncherWindow:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Hydraulic Data Explorer")
        self.root.geometry("1100x700")
        self.root.minsize(1000, 620)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._raw_preview: pd.DataFrame | None = None   # first N rows of CSV
        self._field_vars: dict[str, tk.StringVar] = {}  # cfg_key → StringVar (dropdown value)
        self._filter_vars: dict[str, tk.StringVar] = {}

        self._build_ui()

    # ------------------------------------------------------------------
    # UI BUILD
    # ------------------------------------------------------------------
    def _build_ui(self):
        # ── Header bar ───────────────────────────────────────────────
        header = tk.Frame(self.root, bg=ACCENT, height=56)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="Hydraulic Data Explorer", bg=ACCENT, fg=WHITE,
                 font=("Segoe UI", 14, "bold")).pack(side="left", padx=20, pady=14)

        # ── Main paned area ──────────────────────────────────────────
        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=18, pady=14)

        # Left column: file pick + preview
        left = tk.Frame(main, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(0, 12))

        # Right column: mapping + filters + status
        right = tk.Frame(main, bg=BG, width=420)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        # ── FILE PICKER (left) ────────────────────────────────────────
        self._lbl("CSV FILE", left)
        fp_row = tk.Frame(left, bg=BG)
        fp_row.pack(fill="x", pady=(2, 8))

        self._file_var = tk.StringVar()
        tk.Entry(fp_row, textvariable=self._file_var, font=("Segoe UI", 10),
                 relief="solid", bd=1, bg=WHITE).pack(side="left", fill="x", expand=True,
                                                       ipady=5, padx=(0, 6))
        tk.Button(fp_row, text="Browse…", command=self._browse,
                  bg=WHITE, fg=ACCENT, relief="solid", bd=1,
                  font=("Segoe UI", 10), padx=8, cursor="hand2").pack(side="left", ipady=5)

        # ── CSV PREVIEW (left) ───────────────────────────────────────
        self._lbl("FILE PREVIEW  (first rows of your CSV — use this to fill in the mapping →)", left)
        preview_outer = tk.Frame(left, bg=BORDER, bd=1, relief="solid")
        preview_outer.pack(fill="both", expand=True, pady=(2, 0))

        self._preview_text = tk.Text(
            preview_outer, font=("Consolas", 8), bg="#E9ECEF", fg="#343A40",
            relief="flat", wrap="none", state="disabled",
            xscrollcommand=lambda *a: hbar.set(*a),
            yscrollcommand=lambda *a: vbar.set(*a),
        )
        vbar = tk.Scrollbar(preview_outer, orient="vertical",
                            command=self._preview_text.yview)
        hbar = tk.Scrollbar(preview_outer, orient="horizontal",
                            command=self._preview_text.xview)
        vbar.pack(side="right", fill="y")
        hbar.pack(side="bottom", fill="x")
        self._preview_text.pack(fill="both", expand=True)

        # ── ROW / COLUMN MAPPING (right) ─────────────────────────────
        self._lbl("ROW / COLUMN MAPPING", right)
        tk.Label(right, text="Select which row each field comes from.\nLeave optional fields as '— not in this file —'.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8, "italic"), justify="left",
                 wraplength=290).pack(anchor="w", pady=(0, 6))

        mapping_frame = tk.Frame(right, bg=BG)
        mapping_frame.pack(fill="x")

        self._dropdowns: dict[str, ttk.Combobox] = {}

        for label, key, required in FIELD_DEFS:
            row_f = tk.Frame(mapping_frame, bg=BG)
            row_f.pack(fill="x", pady=3)
            lbl_text = label + (" *" if required else "")
            tk.Label(row_f, text=lbl_text, bg=BG, fg="#343A40",
                     font=("Segoe UI", 9), width=22, anchor="w").pack(side="left")
            var = tk.StringVar(value=NOT_PRESENT)
            self._field_vars[key] = var
            cb = ttk.Combobox(row_f, textvariable=var, state="readonly",
                              font=("Segoe UI", 9), width=38)
            cb["values"] = [NOT_PRESENT]
            cb.pack(side="left")
            self._dropdowns[key] = cb

        # ── FILTERS (right) ───────────────────────────────────────────
        tk.Frame(right, bg=BORDER, height=1).pack(fill="x", pady=10)
        self._lbl("FILTERS  (comma-separated, blank = all)", right)

        for label, key in [("AEPs", "filter_aeps"), ("Scenarios", "filter_scenarios")]:
            ff = tk.Frame(right, bg=BG)
            ff.pack(fill="x", pady=2)
            tk.Label(ff, text=label, bg=BG, fg="#343A40",
                     font=("Segoe UI", 9), width=10, anchor="w").pack(side="left")
            var = tk.StringVar()
            self._filter_vars[key] = var
            tk.Entry(ff, textvariable=var, font=("Segoe UI", 9),
                     relief="solid", bd=1, bg=WHITE, width=20).pack(side="left", ipady=3)

        tk.Label(right, text="e.g.  AEPs: 1p, 2p, 50p\nScenarios: BASELINE, POST_DEV\n(blank = include everything)",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8, "italic"), justify="left").pack(anchor="w", pady=(2, 6))

        tk.Button(right, text="Scan file → auto-fill filters",
                  command=self._scan_filter_values,
                  bg="#E8F4F8", fg=ACCENT, relief="solid", bd=1,
                  font=("Segoe UI", 9, "bold"), cursor="hand2", pady=5
                  ).pack(fill="x", pady=(0, 8))

        # ── Location filter ───────────────────────────────────────────
        tk.Frame(right, bg=BORDER, height=1).pack(fill="x", pady=(0, 8))
        loc_hdr = tk.Frame(right, bg=BG)
        loc_hdr.pack(fill="x")
        self._lbl("LOCATIONS  (select to limit load — none selected = all)", loc_hdr)
        tk.Button(loc_hdr, text="Scan →",
                  command=self._scan_locations,
                  bg="#E8F4F8", fg=ACCENT, relief="solid", bd=1,
                  font=("Segoe UI", 8, "bold"), cursor="hand2", padx=6, pady=2
                  ).pack(side="right")

        # Search box
        self._loc_search_var = tk.StringVar()
        self._loc_search_var.trace_add("write", self._filter_loc_listbox)
        tk.Entry(right, textvariable=self._loc_search_var, font=("Segoe UI", 9),
                 relief="solid", bd=1, bg=WHITE,
                 placeholder_text="Search locations…"
                 ) if False else tk.Entry(right, textvariable=self._loc_search_var,
                                          font=("Segoe UI", 9), relief="solid", bd=1,
                                          bg=WHITE).pack(fill="x", ipady=3, pady=(2, 4))

        # Listbox with scrollbar
        lb_frame = tk.Frame(right, bg=BG)
        lb_frame.pack(fill="x")
        lb_scroll = tk.Scrollbar(lb_frame, orient="vertical")
        self._loc_listbox = tk.Listbox(
            lb_frame, selectmode="multiple", height=8,
            font=("Segoe UI", 9), relief="solid", bd=1,
            bg=WHITE, activestyle="none",
            yscrollcommand=lb_scroll.set,
            exportselection=False,
        )
        lb_scroll.config(command=self._loc_listbox.yview)
        lb_scroll.pack(side="right", fill="y")
        self._loc_listbox.pack(side="left", fill="x", expand=True)
        self._loc_listbox.insert("end", "— scan file to populate —")
        self._loc_listbox.config(state="disabled")

        # Select all / none buttons
        sel_frame = tk.Frame(right, bg=BG)
        sel_frame.pack(fill="x", pady=(3, 0))
        tk.Button(sel_frame, text="Select all", command=self._loc_select_all,
                  bg=WHITE, fg=ACCENT, relief="solid", bd=1,
                  font=("Segoe UI", 8), cursor="hand2").pack(side="left", padx=(0, 4))
        tk.Button(sel_frame, text="Clear all", command=self._loc_clear_all,
                  bg=WHITE, fg=MUTED, relief="solid", bd=1,
                  font=("Segoe UI", 8), cursor="hand2").pack(side="left")

        # ── STATUS (right) ────────────────────────────────────────────
        tk.Frame(right, bg=BORDER, height=1).pack(fill="x", pady=10)
        self._lbl("STATUS", right)

        log_outer = tk.Frame(right, bg="#E9ECEF", relief="solid", bd=1)
        log_outer.pack(fill="both", expand=True)
        self._log_text = tk.Text(log_outer, font=("Consolas", 8), bg="#E9ECEF", fg="#343A40",
                                 relief="flat", state="disabled", wrap="word", padx=6, pady=4)
        self._log_text.pack(fill="both", expand=True)

        # ── PROGRESS + BUTTONS (bottom of right) ─────────────────────
        self._progress = ttk.Progressbar(right, mode="indeterminate")
        self._progress.pack(fill="x", pady=(6, 4))

        btn_f = tk.Frame(right, bg=BG)
        btn_f.pack(fill="x")

        self._load_btn = tk.Button(btn_f, text="Load & Launch", command=self._start_loading,
                                   bg=ACCENT, fg=WHITE, relief="flat",
                                   font=("Segoe UI", 10, "bold"), pady=6, cursor="hand2")
        self._load_btn.pack(fill="x", pady=(0, 4))

        self._open_btn = tk.Button(btn_f, text="Open in Browser", command=self._open_browser,
                                   bg=GREEN, fg=WHITE, relief="flat",
                                   font=("Segoe UI", 10, "bold"), pady=6,
                                   cursor="hand2", state="disabled")
        self._open_btn.pack(fill="x", pady=(0, 4))

        self._stop_btn = tk.Button(btn_f, text="Stop Server", command=self._on_close,
                                   bg=RED, fg=WHITE, relief="flat",
                                   font=("Segoe UI", 10, "bold"), pady=6,
                                   cursor="hand2", state="disabled")
        self._stop_btn.pack(fill="x")

    def _lbl(self, text, parent):
        tk.Label(parent, text=text, bg=BG, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")

    # ------------------------------------------------------------------
    # FILE BROWSE + PREVIEW
    # ------------------------------------------------------------------
    def _browse(self):
        path = filedialog.askopenfilename(
            title="Select CSV file",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        self._file_var.set(path)
        self._preview_text.configure(state="normal")
        self._preview_text.delete("1.0", "end")
        self._preview_text.insert("end", f"Loading preview of  {Path(path).name} …\n")
        self._preview_text.configure(state="disabled")
        self._log(f"Reading file: {Path(path).name}")
        self._start_spinner("Reading CSV headers")
        threading.Thread(target=self._load_preview, args=(Path(path),), daemon=True).start()

    def _load_preview(self, path: Path):
        try:
            col_w = 20

            # Read PREVIEW_ROWS rows in full (all columns) so unique-value snippets are accurate
            # even when values like "50p" only appear thousands of columns in
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                import csv
                reader = csv.reader(fh)
                all_rows = []
                for row in reader:
                    all_rows.append(row)  # keep ALL columns
                    if len(all_rows) >= PREVIEW_ROWS:
                        break

            if not all_rows:
                self.root.after(0, lambda: self._log("Preview: file appears empty."))
                return

            # Build header line using first row's column count
            n_cols = max(len(r) for r in all_rows)
            header_line = "Row │ " + " │ ".join(f"Col {c:<2}".ljust(col_w) for c in range(min(n_cols, 15)))
            sep_line    = "────┼" + "─" * len(header_line)

            def _write(text):
                self.root.after(0, lambda t=text: self._append_preview(t))

            # Clear and write header
            self.root.after(0, lambda: self._reset_preview(header_line + "\n" + sep_line + "\n"))

            for r, row in enumerate(all_rows):
                cells = []
                for c in range(min(15, n_cols)):
                    val = (row[c] if c < len(row) else "").strip()[:col_w].ljust(col_w)
                    cells.append(val)
                _write(f"  {r} │ " + " │ ".join(cells) + "\n")
                time.sleep(0.04)   # small delay so you can see it stream in

            if n_cols > 15:
                _write(f"\n    … {n_cols} columns total — scroll right to see more\n")

            # Build the DataFrame for dropdown population
            import io
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                preview_df = pd.read_csv(io.StringIO("\n".join(
                    ",".join(r) for r in all_rows
                )), header=None)

            self._raw_preview = preview_df
            self._all_rows_full = all_rows  # full-width rows for unique-value scanning
            self.root.after(0, lambda: self._populate_dropdowns(preview_df, n_cols, all_rows))
            self.root.after(0, lambda: self._stop_spinner(f"Preview ready — {n_cols} columns detected"))
            self.root.after(0, lambda: self._log("Assign each field using the dropdowns on the right →"))

        except Exception as exc:
            self.root.after(0, lambda: self._stop_spinner())
            self.root.after(0, lambda: self._log(f"Preview error: {exc}"))

    def _reset_preview(self, text: str):
        self._preview_text.configure(state="normal")
        self._preview_text.delete("1.0", "end")
        self._preview_text.insert("end", text)
        self._preview_text.configure(state="disabled")

    def _append_preview(self, text: str):
        self._preview_text.configure(state="normal")
        self._preview_text.insert("end", text)
        self._preview_text.see("end")
        self._preview_text.configure(state="disabled")

    def _populate_dropdowns(self, df: pd.DataFrame, n_cols: int, all_rows: list[list[str]] | None = None):
        row_options = []
        for r in range(len(df)):
            # Scan full-width row (all_rows) for unique values so "50p"
            # appearing thousands of columns in still shows up in the snippet
            source_row = all_rows[r] if all_rows else []
            seen = []
            seen_set = set()
            for v in source_row:
                v = v.strip()
                if v and v not in seen_set and v.lower() not in ("nan", "none"):
                    seen_set.add(v)
                    seen.append(v)
            n_unique = len(seen_set)
            snippet = ",  ".join(seen[:12])[:100]
            if n_unique > 12:
                snippet += f"  … (+{n_unique - 12} more)"
            row_options.append(f"Row {r}  [{n_unique} unique values]:  {snippet}")

        col_options = []
        for c in range(min(n_cols, 30)):
            snippet = ",  ".join(str(df.iloc[r2, c]).strip() for r2 in range(min(5, len(df)))
                                 if str(df.iloc[r2, c]).strip())[:80]
            col_options.append(f"Col {c}:  {snippet}")

        for label, key, required in FIELD_DEFS:
            cb = self._dropdowns[key]
            if key == "col_time":
                opts = col_options
            elif key == "row_data_start":
                opts = row_options if required else [NOT_PRESENT] + row_options
            else:
                opts = row_options if required else [NOT_PRESENT] + row_options
            cb["values"] = opts
            cb.set(opts[0])

    def _show_preview(self, df: pd.DataFrame):
        pass  # replaced by streaming approach above

        # Populate the mapping dropdowns from actual row content
        n_rows = df.shape[1]
        row_options = []
        for r in range(len(df)):
            snippet = ",  ".join(str(df.iloc[r, c]) for c in range(min(8, n_rows))
                                 if str(df.iloc[r, c]).strip())[:90]
            row_options.append(f"Row {r}:  {snippet}")

        col_options = []
        for c in range(min(n_rows, 30)):
            snippet = ",  ".join(str(df.iloc[r2, c]) for r2 in range(min(5, len(df)))
                                 if str(df.iloc[r2, c]).strip())[:80]
            col_options.append(f"Col {c}:  {snippet}")

        for label, key, required in FIELD_DEFS:
            cb = self._dropdowns[key]
            if key in ("row_data_start",):
                # show row options (which row does data start on)
                opts = [NOT_PRESENT] + row_options if not required else row_options
                cb["values"] = opts
                cb.set(opts[0])
            elif key == "col_time":
                opts = col_options
                cb["values"] = opts
                cb.set(opts[0])
            else:
                opts = ([] if required else [NOT_PRESENT]) + row_options
                cb["values"] = opts
                cb.set(opts[0])

        self._log(f"Preview loaded — {df.shape[0]} header/data rows shown, {df.shape[1]} columns.")
        self._log("Assign each field using the dropdowns on the right, then click Load & Launch.")

    # ------------------------------------------------------------------
    # LOGGING + SPINNER
    # ------------------------------------------------------------------
    def _log(self, msg: str):
        def _do():
            self._log_text.configure(state="normal")
            self._log_text.insert("end", msg + "\n")
            self._log_text.see("end")
            self._log_text.configure(state="disabled")
        self.root.after(0, _do)

    _SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def _start_spinner(self, label: str):
        self._spinner_label = label
        self._spinner_active = True
        self._spinner_idx = 0
        self._spinner_line_written = False
        self._tick_spinner()

    def _tick_spinner(self):
        if not self._spinner_active:
            return
        frame = self._SPINNER_FRAMES[self._spinner_idx % len(self._SPINNER_FRAMES)]
        self._spinner_idx += 1
        msg = f"{frame} {self._spinner_label}…"

        self._log_text.configure(state="normal")
        if self._spinner_line_written:
            # overwrite the last line
            self._log_text.delete("end-2l", "end-1l")
        self._log_text.insert("end-1c", msg + "\n")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")
        self._spinner_line_written = True

        self._spinner_after_id = self.root.after(120, self._tick_spinner)

    def _stop_spinner(self, final_msg: str | None = None):
        self._spinner_active = False
        if hasattr(self, "_spinner_after_id"):
            self.root.after_cancel(self._spinner_after_id)
        # replace spinner line with final message
        self._log_text.configure(state="normal")
        if self._spinner_line_written:
            self._log_text.delete("end-2l", "end-1l")
        if final_msg:
            self._log_text.insert("end-1c", f"✓ {final_msg}\n")
        self._log_text.see("end")
        self._log_text.configure(state="disabled")
        self._spinner_line_written = False

    # ------------------------------------------------------------------
    # BUILD CONFIG FROM DROPDOWNS
    # ------------------------------------------------------------------
    def _parse_row_idx(self, key: str) -> int | None:
        val = self._field_vars[key].get()
        if val == NOT_PRESENT or not val.strip():
            return None
        # Format is "Row 3  [N unique values]:  …" or "Col 0:  …"
        # Just grab the first integer in the string
        m = re.search(r'\d+', val)
        return int(m.group()) if m else None

    def _build_cfg(self) -> dict:
        cfg = {
            "row_param":       self._parse_row_idx("row_param"),
            "row_location":    self._parse_row_idx("row_location"),
            "row_model":       self._parse_row_idx("row_model"),
            "row_scenario":    self._parse_row_idx("row_scenario"),
            "row_climate":     self._parse_row_idx("row_climate"),
            "row_aep":         self._parse_row_idx("row_aep"),
            "row_data_start":  self._parse_row_idx("row_data_start"),
            "col_time":        self._parse_row_idx("col_time"),
            "filter_aeps":      parse_filter_list(self._filter_vars["filter_aeps"].get()),
            "filter_scenarios": parse_filter_list(self._filter_vars["filter_scenarios"].get()),
            "filter_locations": {self._loc_listbox.get(i) for i in self._loc_listbox.curselection()},
        }
        # Defaults if not set
        if cfg["row_data_start"] is None:
            cfg["row_data_start"] = 6
        if cfg["col_time"] is None:
            cfg["col_time"] = 0
        return cfg

    # ------------------------------------------------------------------
    # LOCATION LISTBOX HELPERS
    # ------------------------------------------------------------------
    def _loc_select_all(self):
        self._loc_listbox.select_set(0, "end")

    def _loc_clear_all(self):
        self._loc_listbox.selection_clear(0, "end")

    def _filter_loc_listbox(self, *_):
        query = self._loc_search_var.get().strip().lower()
        all_locs = getattr(self, "_all_locations", [])
        self._loc_listbox.delete(0, "end")
        for loc in all_locs:
            if query in loc.lower():
                self._loc_listbox.insert("end", loc)

    def _scan_locations(self):
        path_str = self._file_var.get().strip()
        if not path_str:
            messagebox.showwarning("No file", "Browse to a CSV file first.")
            return
        loc_idx = self._parse_row_idx("row_location")
        if loc_idx is None:
            messagebox.showwarning("No mapping", "Assign the Location row in the mapping first.")
            return
        self._log("Scanning file for unique locations…")
        self._start_spinner("Scanning locations")
        threading.Thread(target=self._scan_locations_worker,
                         args=(Path(path_str), loc_idx), daemon=True).start()

    def _scan_locations_worker(self, path: Path, loc_row: int):
        try:
            import csv
            loc_vals = []
            seen = set()
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                reader = csv.reader(fh)
                for r_idx, row in enumerate(reader):
                    if r_idx == loc_row:
                        for v in row:
                            v = v.strip()
                            if v and v not in seen and v.lower() not in ("location", "loc", ""):
                                seen.add(v)
                                loc_vals.append(v)
                    if r_idx > loc_row:
                        break

            loc_vals.sort()
            self._all_locations = loc_vals

            def _apply():
                self._loc_listbox.config(state="normal")
                self._loc_listbox.delete(0, "end")
                for loc in loc_vals:
                    self._loc_listbox.insert("end", loc)
                self._loc_listbox.select_set(0, "end")  # select all by default
                self._stop_spinner(f"Found {len(loc_vals)} locations — all selected")

            self.root.after(0, _apply)

        except Exception as exc:
            self.root.after(0, lambda: self._stop_spinner())
            self.root.after(0, lambda: self._log(f"Location scan error: {exc}"))

    # ------------------------------------------------------------------
    # SCAN FILE FOR FILTER VALUES
    # ------------------------------------------------------------------
    def _scan_filter_values(self):
        path_str = self._file_var.get().strip()
        if not path_str:
            messagebox.showwarning("No file", "Browse to a CSV file first.")
            return
        if self._raw_preview is None:
            messagebox.showwarning("No preview", "Wait for the preview to finish loading first.")
            return

        aep_idx      = self._parse_row_idx("row_aep")
        scenario_idx = self._parse_row_idx("row_scenario")

        if aep_idx is None and scenario_idx is None:
            messagebox.showinfo("Nothing to scan",
                                "Mark AEP and/or Scenario rows in the mapping first.")
            return

        self._log("Scanning file for unique AEP / Scenario values…")
        self._start_spinner("Scanning")
        threading.Thread(target=self._scan_worker,
                         args=(Path(path_str), aep_idx, scenario_idx),
                         daemon=True).start()

    def _scan_worker(self, path: Path, aep_row: int | None, scenario_row: int | None):
        try:
            import csv
            aep_vals      = set()
            scenario_vals = set()

            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                reader = csv.reader(fh)
                for r_idx, row in enumerate(reader):
                    if aep_row is not None and r_idx == aep_row:
                        for v in row:
                            v = v.strip()
                            nv = norm_aep(v)
                            # Skip the header label itself (e.g. "aep", "aep_%")
                            if nv and nv not in ("aep", "aep_", "exceedance"):
                                aep_vals.add(nv)
                    if scenario_row is not None and r_idx == scenario_row:
                        for v in row:
                            v = v.strip()
                            nv = norm_scenario(v)
                            # Skip the header label itself
                            if nv and nv not in ("SCENARIO", "SCENARIOS", "TYPE"):
                                scenario_vals.add(nv)
                    # Stop once we've read past the header rows we need
                    max_row = max(r for r in [aep_row, scenario_row] if r is not None)
                    if r_idx > max_row:
                        break

            def _apply():
                if aep_vals:
                    self._filter_vars["filter_aeps"].set(", ".join(sorted(aep_vals)))
                if scenario_vals:
                    self._filter_vars["filter_scenarios"].set(", ".join(sorted(scenario_vals)))
                parts = []
                if aep_vals:      parts.append(f"AEPs: {', '.join(sorted(aep_vals))}")
                if scenario_vals: parts.append(f"Scenarios: {', '.join(sorted(scenario_vals))}")
                self._stop_spinner("Scan complete — " + "   |   ".join(parts))

            self.root.after(0, _apply)

        except Exception as exc:
            self.root.after(0, lambda: self._stop_spinner())
            self.root.after(0, lambda: self._log(f"Scan error: {exc}"))

    # ------------------------------------------------------------------
    # LOAD & LAUNCH
    # ------------------------------------------------------------------
    def _start_loading(self):
        path_str = self._file_var.get().strip()
        if not path_str:
            messagebox.showwarning("No file", "Please select a CSV file first.")
            return
        path = Path(path_str)
        if not path.exists():
            messagebox.showerror("File not found", f"Cannot find:\n{path}")
            return
        cfg = self._build_cfg()
        if cfg["row_param"] is None or cfg["row_location"] is None:
            messagebox.showwarning(
                "Mapping incomplete",
                "Parameter/Type and Location rows are required.\n"
                "Please assign them in the mapping panel."
            )
            return

        self._load_btn.configure(state="disabled")
        self._progress.start(12)
        self._start_spinner("Parsing CSV data")
        threading.Thread(target=self._load_worker, args=(path, cfg), daemon=True).start()

    def _load_worker(self, path: Path, cfg: dict):
        try:
            long_df, summary_df = load_csv(path, cfg, self._log)
            self.root.after(0, lambda: self._stop_spinner("Data loaded"))
            self.root.after(0, lambda: self._start_spinner("Building summary table"))
            app = build_dash_app(long_df, summary_df)
            self.root.after(0, lambda: self._stop_spinner("Summary ready"))
            self.root.after(0, lambda: self._start_spinner("Starting server"))
            self._log(f"Starting server on http://127.0.0.1:{PORT} …")

            def run_server():
                import logging
                logging.getLogger("werkzeug").setLevel(logging.ERROR)
                app.run(debug=False, port=PORT, use_reloader=False)

            threading.Thread(target=run_server, daemon=True).start()
            time.sleep(1.5)

            self.root.after(0, lambda: self._stop_spinner(f"Server ready — http://127.0.0.1:{PORT}"))
            self.root.after(0, self._on_server_ready)

        except Exception as exc:
            self.root.after(0, lambda: self._stop_spinner())
            self._log(f"ERROR: {exc}")
            self.root.after(0, lambda: self._load_btn.configure(state="normal"))
            self.root.after(0, self._progress.stop)

    def _on_server_ready(self):
        self._progress.stop()
        self._progress.configure(mode="determinate", value=100)
        self._open_btn.configure(state="normal")
        self._stop_btn.configure(state="normal")
        webbrowser.open(f"http://127.0.0.1:{PORT}")

    def _open_browser(self):
        webbrowser.open(f"http://127.0.0.1:{PORT}")

    def _on_close(self):
        self.root.destroy()
        sys.exit(0)

    def run(self):
        self.root.mainloop()


# =========================================================
# ENTRY POINT
# =========================================================
if __name__ == "__main__":
    LauncherWindow().run()

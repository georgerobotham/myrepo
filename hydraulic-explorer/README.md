# Hydraulic Explorer

A generic viewer for wide-format hydraulic time-series CSVs (velocity,
discharge, water level, etc. across locations, models, scenarios, climate
cases and AEPs). Launch it, tell it which CSV rows hold what via a startup
window, and it opens an interactive viewer in your browser — filters, an
overlaid hydrograph chart, and a summary table.

Not tied to any particular project's file format: the startup window maps
your CSV's row structure (which row holds the parameter name, location,
model, scenario, climate case, AEP, where the time-series data starts) before
loading, so it generalises across differently-laid-out inputs.

## Running it

**From source:**

```bash
pip install -r requirements.txt
python hydraulic_explorer.py
```

A configuration window opens first — pick your CSV, map its rows, set any
AEP/scenario filters, then load. Once loaded, a local web viewer opens
automatically in your browser.

**Windows, no Python needed:** build a standalone `.exe` with
`pyinstaller "Hydraulic Explorer.spec"` and double-click the result in
`dist/`.

## Files

- `hydraulic_explorer.py` — everything: the startup config window (tkinter),
  CSV parsing, and the Dash-based viewer app.
- `Hydraulic Explorer.spec` — PyInstaller spec for building the standalone
  `.exe`.

## Requirements

Python 3.10+, `pandas`, `matplotlib`, `numpy`, `dash`. See `requirements.txt`.

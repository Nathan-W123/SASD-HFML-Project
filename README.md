# SASD HFML Pipeline Automation

A locally-run Streamlit desktop application that automates the monthly High Frequency Mainline (HFML) data pipeline for SASD. Each user runs the app on their own Windows machine with ArcGIS Pro installed. All shared data files live on a network drive.

## Pipeline Phases

- **Phase 1** — Connect to SQL database, run query, export results, import into Excel as a new `import` sheet
- **Phase 2** — Excel manipulation: detect added/removed MLs and frequency changes, rebuild the working sheet
- **Phase 3** — Sync Excel data to ArcGIS attribute table via Excel To Table and Append with upsert logic
- **Phase 4** — For each new HFML, append associated ML, LL, and upstream parcel features to the dataset
- **Phase 5** — Generate and export a PDF map for each new HFML using the appropriate map template

## Project Structure

```
phase1/   - SQL query and Excel import script
phase2/   - Excel manipulation logic
phase3/   - ArcGIS attribute table sync
phase4/   - Upstream network trace and feature append
phase5/   - PDF map generation and export
docs/     - Project ruleset (CLAUDE.md) and pipeline workflow (PIPELINE.md)
```

## Tech Stack

- **UI:** Streamlit (runs locally in browser, launched via `.bat` file)
- **GIS:** ArcGIS Pro + `arcpy`
- **Database:** SQL Server via `pyodbc`
- **Excel:** `openpyxl` / `pandas`
- **Config:** `config.json` on the shared network drive

## Setup

All shared paths and SQL connection details are defined in `config.json` on the network drive. Launch the app via `launch.bat` — no CLI knowledge required.

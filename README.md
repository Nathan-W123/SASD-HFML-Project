# SASD HFML Pipeline Automation

A locally-run Streamlit desktop application that automates the monthly High Frequency Mainline (HFML) data pipeline for SASD. Each user runs the app on their own Windows machine with ArcGIS Pro installed.

## Pipeline Phases

- **Phase 1** - Connect to SQL database, run query, export results, import into Excel as a new `import` sheet
- **Phase 2** - Excel manipulation: detect added/removed MLs and frequency changes, rebuild the working sheet
- **Phase 3** - Sync Excel data to ArcGIS attribute table via Excel To Table and Append with upsert logic
- **Phase 4** - For each new HFML, recursively trace and append all upstream ML segments, then append all LLs contacting those MLs, then append all parcels intersecting those LLs
- **Phase 5** - Generate and export a PDF map for each new HFML using the appropriate map template

## Project Structure

```
backend/  - phase1-5 scripts
frontend/ - Streamlit UI
docs/     - project notes and pipeline workflow
data/     - local SQL, Excel, GIS, template, and PDF files
```

## Tech Stack

- **UI:** Streamlit
- **GIS:** ArcGIS Pro + `arcpy`
- **Database:** SQL Server via `pyodbc`
- **Excel:** `openpyxl` / `pandas`
- **Config:** project-local `config.json`

## Setup

All local paths and SQL connection details are defined in `config.json`. By default, the app expects its working files under `data/` in this project folder. Launch the app via `launch.bat` - no CLI knowledge required.

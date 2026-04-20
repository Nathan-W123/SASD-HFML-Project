# Project: Monthly GIS Pipeline Automation App

## Overview
A locally-run Streamlit desktop application that automates a monthly 5-phase data pipeline involving SQL, Excel, and ArcGIS Pro. Each user runs the app on their own Windows machine with ArcGIS Pro installed. All data files live on a shared network drive.

## Tech Stack
- **Frontend/UI:** Streamlit (runs locally in browser)
- **GIS:** ArcGIS Pro with `arcpy` (available via ArcGIS Pro's conda environment)
- **Database:** SQL Server (single shared database, credentials in config)
- **Excel:** `openpyxl` and/or `pandas`
- **Launcher:** `.bat` file that activates ArcGIS Pro's conda environment and launches Streamlit
- **Config:** `config.json` on the shared network drive for all shared paths and SQL connection details

## Pipeline Phases
Run sequentially by a single user. Each phase is a discrete Python subscript.

- **Phase 1:** Connect to SQL database, run query, export result, import CSV into a new Excel sheet named `import`
- **Phase 2:** Excel manipulation — add vlookup-equivalent logic to detect added/removed MLs and frequency changes, create new sheet with current data, delete old sheets, rename to match original sheet name
- **Phase 3:** Sync Excel data to ArcGIS attribute table — run Excel To Table geoprocessing, use Append tool with field mapping and upsert logic keyed on unique ID field
- **Phase 4:** For each new HFML, append associated ML, LL, and upstream parcels to the dataset
- **Phase 5:** For each new appended dataset, generate a PDF map from the appropriate template (vertical or horizontal based on data fit) and export to the shared output directory

## File & Path Rules
- All shared files (Excel workbook, ArcGIS project, map templates, PDF output folder) live on a shared network drive
- All paths are defined in `config.json` — never hardcode paths in scripts
- Excel workbook structure is constant — do not add logic to handle variable schemas
- PDF output directory is a fixed shared folder defined in config

## Architecture Rules
- Each phase is a separate Python function or subscript file, called from the main Streamlit app
- The Streamlit app is a single-page UI with one button per phase plus a "Run All" button
- All terminal/log output from each phase must be captured and displayed in a Streamlit log panel in real time
- Phases must run sequentially — do not parallelize
- Do not add web servers, APIs, or any remote hosting logic — this is local-only

## ArcGIS / arcpy Rules
- Always use the ArcGIS Pro conda Python environment — the `.bat` launcher handles environment activation
- Use `arcpy` for all GIS operations — do not use open-source GIS libraries (geopandas, fiona, etc.)
- Map template selection (vertical vs. horizontal) is determined programmatically based on data geometry fit — both `.aprx` templates already exist
- Do not modify the `.aprx` template files — clone them per export

## Coding Rules
- No comments unless the WHY is non-obvious
- No docstrings
- No error handling for scenarios that cannot happen
- Validate only at system boundaries (SQL connection, file path existence, arcpy license check)
- Do not add features beyond what each phase requires
- Do not abstract prematurely — three similar lines is better than a premature helper
- Keep each phase script self-contained and independently runnable for debugging

## Configuration
`config.json` contains:
- SQL connection string
- Path to shared Excel workbook
- Path to ArcGIS project (`.aprx`)
- Path to vertical map template
- Path to horizontal map template
- Path to PDF export output directory
- Unique ID field name used for upsert matching

## Distribution
- The entire app folder lives on the shared network drive
- Users launch via `launch.bat` — no CLI knowledge required
- To update the app, replace files in the shared folder — no reinstall needed

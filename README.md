# SASD HFML Pipeline Automation

An ArcGIS Pro Python Toolbox (`SASD_HFML.pyt`) that automates the monthly High Frequency Mainline (HFML) data pipeline for SASD. Each user runs the **Run HFML Pipeline** tool from inside ArcGIS Pro on their own Windows machine.

## Pipeline Phases

- **Phase 1** - Connect to SQL database, run query, export results, import into Excel as a new `import` sheet
- **Phase 2** - Excel manipulation: detect added/removed MLs and frequency changes, rebuild the working sheet
- **Phase 3** - Sync Excel data to ArcGIS attribute table via Excel To Table and Append with upsert logic
- **Phase 4** - For the next 5 new HFMLs (beta limit), one at a time: trace and append upstream MLs, contacting LLs, and parcels; export that HFML's PDF map and save the map as a project into a dated run folder (`PDF_Output/<YYYY-MM-DD HH-MM>/`, projects under `Temp GIS Maps/`); then delete the appended features
- **Phase 5** - Verify every HFML mapped by Phase 4 has its PDF

## Project Structure

```
SASD_HFML.pyt - ArcGIS Pro toolbox (Run HFML Pipeline tool)
backend/  - phase1-5 scripts
docs/     - project notes and pipeline workflow
data/     - local SQL, Excel, GIS, template, and PDF files
```

## Tech Stack

- **UI:** ArcGIS Pro Python Toolbox (Geoprocessing pane)
- **GIS:** ArcGIS Pro + `arcpy`
- **Database:** SQL Server via `pyodbc`
- **Excel:** `openpyxl` / `pandas`
- **Config:** project-local `config.json`

## Setup

All local paths and SQL connection details are defined in `config.json`. Relative paths resolve against this project folder, so keep `SASD_HFML.pyt` in the project root next to `config.json`.

### One-time Python setup (per machine)

The pipeline runs inside ArcGIS Pro's Python environment, which needs `pyodbc`, `pandas` and `openpyxl`. `pandas` and `openpyxl` normally ship with Pro; `pyodbc` usually does not. Pro's default `arcgispro-py3` environment is read-only for most users, so:

1. In ArcGIS Pro open **Settings > Package Manager**.
2. Clone the default environment, then activate the clone.
3. Add `pyodbc` to the clone (and `pandas` / `openpyxl` if missing).
4. Restart ArcGIS Pro.

### Running the pipeline

1. In ArcGIS Pro, open the **Catalog** pane, right-click **Toolboxes > Add Toolbox**, and select `SASD_HFML.pyt`. Save the project to keep it there.
2. Expand the toolbox and double-click **Run HFML Pipeline**. All five phases are checked under **Phases to run**; uncheck any you want to skip (e.g. to rerun a single phase), then click **Run**.
3. Progress and log output appear in the Geoprocessing pane (and under **View Details** / Geoprocessing History). Checked phases run in order 1 through 5; the tool stops at the first failed phase and reports the error.

Edits to the backend scripts take effect on the next run without restarting Pro. After editing `SASD_HFML.pyt` itself, right-click the toolbox and choose **Refresh**.

## Deploying code to the shared network folder

The shared network that hosts the GIS project has no git access, so the shared
copy of the code is updated by file copy, not by `git pull`. GitHub stays the
source of truth; a desktop with a clone is the staging copy; the shared folder
is a deploy target.

`deploy.bat` does that copy. Run it from inside the project folder:

```bat
deploy.bat "\\sdfiles\Shared\...\High Frequency PM Project\Code" /L
deploy.bat "\\sdfiles\Shared\...\High Frequency PM Project\Code"
```

The first line is a dry run (`/L`) that writes nothing and lists what would
change — always worth doing first. The second performs the copy.

What it copies: only the files this project tracks (`SASD_HFML.pyt`,
`backend\`, `config_loader.py`, `docs\`, `requirements.txt`, `README.md`).
`data\`, `PDF_Output\`, `Temp GIS Maps\`, geodatabases and anything else
already sitting in the destination are never touched, and the script never
deletes anything — files in the destination that the project no longer has are
listed at the end for you to remove by hand.

`config.json` is deliberately **not** copied, because the shared copy's paths
may have been adjusted locally and overwriting it would silently repoint the
tool. When a config change really does need to go out, diff the two files and
apply the changed keys, or pass `/CONFIG` to overwrite the destination's copy
outright.

Each deployment writes a `VERSION.txt` into the destination recording the
commit, branch, time, and who ran it, so you can see at a glance how far behind
the shared copy is.

### If no single machine can see both GitHub and the share

Deploy in two hops. On the machine with the clone, deploy to a staging folder
(a USB drive, for example):

```bat
deploy.bat "E:\hfml-staging"
```

Carry that folder across, then run the copy of `deploy.bat` inside it:

```bat
E:\hfml-staging\deploy.bat "\\sdfiles\Shared\...\High Frequency PM Project\Code"
```

The staged copy carries its own file list (`deploy_manifest.txt`) and version
stamp, so the second hop needs no git installed anywhere near the share.

### Before deploying

Have everyone close ArcGIS Pro — it holds locks on the project and geodatabase
and reads `SASD_HFML.pyt` at run time. Afterwards each user right-clicks the
toolbox in the Catalog pane and chooses **Refresh** to pick up the new `.pyt`;
changes under `backend\` take effect on the next run with no refresh needed.

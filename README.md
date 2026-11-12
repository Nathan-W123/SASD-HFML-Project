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

## Phase 4 caching

Phase 4 used to rebuild its directed pipe network once per HFML, each time
selecting every pipe within 50,000 feet and reading those geometries over the
wire from `\\pwnb1`. With five HFMLs that was five near-full reads of the
gravity main layer per run.

It now reads the share once, when the source actually changes:

- the ML source is copied to a local file geodatabase,
- the directed network is built once over its full extent and pickled,
- both are reused until the source's feature count or geodatabase timestamps
  change (or `p4_cache_max_age_days` passes),
- the assetnum lookup asks the source for the assetnums being mapped instead of
  reading every row.

The cache lives in the system temp folder by default. Nothing needs to be added
to `config.json` — these keys are optional and default to on:

| key | default | what it does |
| --- | --- | --- |
| `p4_cache_enabled` | `true` | `false` restores the old per-HFML build |
| `p4_cache_dir` | `""` | absolute local path; empty means `%LOCALAPPDATA%\SASD_HFML\phase4_cache` |
| `p4_cache_max_age_days` | `7` | rebuild this often even if the source looks unchanged; `0` disables |
| `p4_cache_local_ml_copy` | `true` | copy the ML source to local disk |
| `p4_filter_assetnum_scan` | `true` | filter the assetnum lookup in the query |
| `p4_node_snap_tolerance_feet` | `0.1` | pipe ends this far apart count as one manhole; `0` restores exact matching |
| `p4_report_network_gaps` | `true` | log how close manholes sit without sharing a key (read-only) |
| `p4_report_trace_gaps` | `true` | log unconnected pipe ends at the edge of each trace (read-only) |

### One effect on the maps

The cached network covers the whole ML source, so a trace is no longer cut off
at the 50,000-foot buffer edge. An HFML whose upstream run fits inside that
buffer traces identically; one with a longer run now traces to its real end,
which is the correct result but a change from previous months. Compare a few
PDFs against last month's before trusting a batch.

## Finding out where Phase 4 spends its time

Phase 4 logs how long each step takes, per HFML and summed for the run:

```
  Timing for 9895 — 142.3s total: map export (separate process) 96.1s, select parcels ...
Phase 4 time by step (714.8s across all HFMLs):
    map export (separate process): 480.5s (67%)
    ...
```

The export runs in its own process, which logs its own three parts (arcpy
import, observations query, building and exporting the map). The difference
between Phase 4's figure for that step and those three is the cost of starting
a Python interpreter and importing the module — paid once per HFML.

Note what the caching did and did not buy. Copying the ML source locally and
building the network take about 18 seconds between them, so eliminating four
rebuilds per run saves under a minute. The remaining time is in the per-HFML
work: the lateral and parcel sources are still read over the network, every
append and the finished PDF are written to the share, and a fresh interpreter
starts for each map. Read the timings before optimising any of it.

## Why a branch of mainlines goes missing

The trace matches manholes by exact key. Sewer data is rarely snapped to the
thousandth of a foot, so two pipe ends a hundredth of a foot apart used to be
two different manholes — the chain stopped there and every pipe above the gap
was silently absent from the map, along with the laterals and parcels that would
have come with them. A whole branch disappearing at once is the signature.

Three things now address it:

- `p4_node_snap_tolerance_feet` (default 0.1) snaps nearby pipe ends onto one
  manhole when the network is built. Raising it closes larger gaps but also
  merges manholes that really are that close, which invents connections — raise
  it no further than the gap distances the log reports.
- When a trace finishes, any unconnected pipe end within 2 feet of an edge of
  the trace is logged with its OID and distance. That is what a cut-off branch
  looks like, and it fires for partial traces, not just empty ones.
- When the network cache is built, the spread of manhole-to-manhole distances
  is logged in 0.01 / 0.1 / 0.5 / 1 foot buckets. That is the measurement that
  should pick the tolerance above.

Separately, an HFML digitized against the flow was traced from the wrong end
when neither end drained anywhere. `_hfml_ends` now breaks that tie on which
end pipes arrive at. A genuine top-of-system HFML is untouched, because it has
a downstream main leaving its downstream end and so never reaches that test.

## Why a map draws fewer features than were appended

Map layers are filtered to the camera extent, and the camera is built to contain
everything Phase 4 appended, so the two counts should agree. When they don't,
the export now says so: the layer report flags any layer drawing fewer features
than were appended this run — or more, which means rows an earlier run left
behind in `Data.gdb` are being drawn on this map — and names any appended
feature that falls outside the map view or has no geometry in its source.

### Object IDs

Copying features to a new geodatabase renumbers OBJECTID. Phase 4 carries ML
object IDs through tracing, appending and the map export, so when the local copy
is in use every one of those steps reads that same copy — the export subprocess
is told which path to use via `source_overrides` in its job record. Changing one
of those call sites to read `config["p4_ml_source_path"]` directly would draw
maps from whatever features happen to hold those IDs remotely.

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

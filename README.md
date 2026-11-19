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

Edits to the backend scripts — including helper modules such as `backend/phase4/network_cache.py` — take effect on the next run without restarting Pro: the toolbox reloads them at the start of every run. After editing `SASD_HFML.pyt` itself, right-click the toolbox and choose **Refresh**.

## Tests

```
python tests/run_all.py          # ~70s
python tests/run_all.py --slow   # also proves the comparison test can fail (~4 min more)
```

No ArcGIS needed: `tests/stub_arcpy.py` stands in for arcpy with real 2D
geometry and arcpy's selection rules. Any Python 3.9+ works, including ArcGIS
Pro's. Each test is a plain script ending in a `FAILURES:` line.

The comparison tests (`test_equivalence`, `test_status_and_appends`,
`test_trace_status`) run the current code and the code from before the
lateral/parcel rewrite on the same generated sewer networks and require
identical results. The old code is read from git history (`tests/baseline.py`),
so those tests need the git clone; on a copy without `.git` they skip.

The stand-in is a model of arcpy, not arcpy. The tests show the new code
matches the old under its rules; the real runs are what confirm the rules. One
rule was wrong at first — whether a cursor on a layer with an empty selection
returns everything or nothing — and was corrected from a real run's log.

`deploy.bat` does not copy `tests\` to the shared folder.

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
| `p4_cache_local_ll_copy` | `true` | copy the lateral source to local disk |
| `p4_cache_local_parcels_copy` | `true` | copy the parcel source to local disk |
| `p4_cache_min_free_gb` | `20` | skip a local copy (and use the remote source) below this much free space |
| `p4_filter_assetnum_scan` | `true` | filter the assetnum lookup in the query |
| `p4_node_snap_tolerance_feet` | `0.1` | pipe ends this far apart count as one manhole; `0` restores exact matching |
| `p4_report_network_gaps` | `true` | log how close manholes sit without sharing a key (read-only) |
| `p4_report_trace_gaps` | `true` | log unconnected pipe ends at the edge of each trace (read-only) |
| `p4_ll_status_filter` | `"STATUS IN ('OPERATING', 'TOBEMOD')"` | only laterals matching this are selected, so their parcels follow; empty selects every status |
| `p4_ml_status_filter` | `"STATUS = 'OPERATING'"` | the upstream trace only follows mains matching this; empty follows every main |
| `p4_export_reuse_process` | `true` | export every map from one Python process; `false` starts one per map as before |

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

A measured run (2026-11-12, five HFMLs, 884s of Phase 4) broke down as:

```
map export (separate process):  342.2s (39%)
select parcels (remote):        278.3s (31%)
select LLs (remote):            171.9s (19%)
appends:                         85.0s (10%)
everything else:                  6.5s  (1%)
```

The selects were flat with respect to data volume — 4 laterals took as long as
226. Copying those sources to local disk barely moved them, and instrumenting
inside the two functions showed why: the spatial selects over the whole lateral
and parcel layers cost **0.7s and 0.5s across the entire run**. The time was in
building scratch feature classes for the lateral endpoints — ~17–20s per call,
twice per HFML, whatever the data volume — plus deleting them again. That is
fixed geoprocessing overhead against the legacy `in_memory` workspace, so the
scratch workspace is now the modern `memory`, with `in_memory` as a fallback.

That did not move it. Timing each tool call individually (2026-11-17) showed
the real pattern: **every geoprocessing tool call costs 3–6 seconds whatever it
does** — CreateFeatureclass plus two AddFields ~16s, each Delete ~3s, each
Append 5–9s — while the row inserts and Describes between them cost 0.0s.
Phase 4 makes dozens of tool calls per HFML.

That floor is Pro writing geoprocessing history and lineage metadata on every
call, with the project on a network share. Phase 4 and the export process now
switch off history logging, metadata logging and adding outputs to the map for
their duration, restoring the previous settings afterwards. The log line
`Geoprocessing bookkeeping off for this phase` confirms which were switched.

It did not (2026-11-17: logging confirmed off, per-call cost unchanged). The
split that held across every run is that schema-changing tools — create a
feature class, add a field, delete — cost 3–6s each, while selections and cursor
reads cost almost nothing. So the lateral and parcel selection no longer makes
schema changes at all:

- the one-foot endpoint test runs on geometry objects in Python
  (`_ends_near_lines`) instead of building point feature classes to select by;
- the feature layers it selects on are made once per run (`_LayerPool`) and
  deleted once, instead of five to seven per HFML;
- selections are read from the selection set itself (`_selected_oids`) rather
  than by making a new layer from a selected one — a tool given a layer with an
  empty selection uses every feature, so when no lateral was near a trace the
  old code built its scratch endpoint table from all ~325,000 laterals.

Per HFML this takes the schema-changing calls from 19 to 0. Measured on
2026-11-17 the two select steps went from 455s to 4.4s a run, and Phase 4
from 1014s to 506s.

The map export was then three quarters of what remained, and timing inside it
showed 50s of every ~70s map was clearing selections — a geoprocessing call on
every layer in the project, though the export opens a fresh copy of the project
in its own process where nothing is normally selected. It now asks each layer
whether it has a selection first (a property read) and clears only those.

Two more geoprocessing-heavy steps were then rewritten the same way:

- **Appends** copy rows with a search cursor into an insert cursor instead of
  the Append tool (4–9s per call measured). Fields are matched by name ignoring
  case, as Append NO_TEST does. Only used when source and destination share a
  coordinate system, since a cursor does not reproject; if a copy fails
  part-way the rows it inserted are deleted and that destination falls back to
  Append for the rest of the run.
- **Map-view filtering** in the export reads each trace layer's dataset through
  the layer's saved definition query and keeps features not disjoint from the
  view, instead of a select-by-location plus clear per layer (~10s a map). Any
  failure falls back to the tool method for that layer.

Both were checked against the code they replace on generated data before
shipping.

### The map export

Measured on 2026-11-17, each map's export took ~38s: ~15s clearing two
selections saved in the map project, ~7.5s starting Python and importing arcpy,
~5s scoping layers to the view, and ~2.5s actually exporting the PDF. Two
changes, each with a fallback to the previous behaviour:

- **One export process for the run.** Layout export fails inside the ArcGIS
  Pro process, so maps are exported by a separate `python.exe` — previously one
  started per map. One process now serves every map, paying the arcpy import
  once. Each map still opens its own fresh copy of the project and gets its own
  copy of the config, so nothing carries from one map to the next. If the
  process dies mid-map it is replaced and that map retried once; it is also
  replaced every 25 maps so memory cannot build up. `p4_export_reuse_process:
  false` restores one process per map.
- **Selections cleared without the tool.** A layer with a saved selection is
  cleared with `Layer.setSelectionSet`, then checked; only if it is still
  selected does the selection tool run, as before. A map is never exported
  with a stale selection either way. Clearing the selections in the project
  and saving it removes the step entirely.

### When PM_Mainlines briefly reads as missing

Twice (2026-11-17, 2026-11-19) the share has answered "PM_Mainlines does not
exist" to the first read after Phase 3 rewrote that table — once three minutes
later — then served it normally seconds afterwards. Every read of it now
retries, clearing the workspace cache, after 2s, 5s and 10s.

The export reads this PM's row once per map (`_read_pm_row`) and uses it for
the figure label, the callout and, for a new HFML, the map extent. If it still
cannot be read, the map fails — it is rolled back and retried next run —
rather than being exported with the HFML number in place of the gridno or an
empty callout, which is what the previous fallbacks did and which would then
have been marked done and never redone.

### Appended features that are not drawn

The export narrows each trace layer to the map view by reading the layer and
replacing its definition query with a list of IDs. That read goes through any
definition query saved on the layer in the project, so a saved filter — on
lateral status or type, say — silently removes appended features from every
map. It explains features going undrawn with no geometry or coordinate-system
problem, and two layers on the same dataset drawing different counts. The
export now logs any saved query it replaces:

```
Layer 'Lower_Laterals' has a definition query saved in the project: '...' — appended features it excludes are not drawn
```

Those queries turned out to be status filters (2026-11-17):

```
Lower_Laterals:              STATUS in( 'OPERATING', 'TOBEMOD')
Trace Upstream High Freq PM: STATUS = 'OPERATING'
```

So laterals that were not in service were selected and appended, then hidden
by the template — while their parcels, which carry no such filter, were still
drawn. Phase 4 now applies the same filter when it selects laterals
(`p4_ll_status_filter`), so a non-operating lateral is never selected and
neither is its parcel. The filter is checked once at the start of Phase 4; one
that is invalid, or matches no laterals at all, is switched off with a warning
rather than silently emptying every map.

Mains get the same treatment. `Trace Upstream High Freq PM` keeps only
`STATUS = 'OPERATING'`, so the upstream trace now only follows mains matching
`p4_ml_status_filter`: a non-operating main is not mapped, and neither is
anything reached only through it — the mains above it, and the laterals and
parcels on those. The HFML itself is always mapped. Each stop is logged:

```
Stopped at 1 main(s) outside the main status filter — not mapped, and nothing upstream of them is: [...]
```

**Keep each pair in step.** If the filter on `Lower_Laterals` or
`Trace Upstream High Freq PM` in the map project changes, change
`p4_ll_status_filter` or `p4_ml_status_filter` to match. If they drift apart,
the "N of the M appended this run are not drawn" warnings come back, which is
the signal.

### Leftover rows in the trace tables

Each map's rows are deleted from `Trace_SSGRAVITYMAIN`, `Lower_Laterals` and
`Parcels_trace` once it is exported, so those tables should be empty between
maps. Rows left by a run that did not finish cleaning up were drawn on every
later map covering their area. Phase 4 now empties the three tables before its
first map and logs what it removed. `PMs_SSGRAVITYMAIN` is never touched — a
seed left there marks its HFML as already mapped. Like the per-map cleanup,
this assumes one pipeline run at a time. The rewrite was
checked against the previous implementation on 300 generated networks covering
the edge cases — ends at exactly the search distance, laterals crossing a main
mid-span, laterals tied in only at the downstream manhole, parcels on shared
edges, ends just outside any parcel — and selected identical laterals and
parcels with identical log lines.

Interpreter startup turned out to be negligible: of 68s per export, 5.5s was
importing arcpy and 61s was building and exporting the map, leaving about 2s of
process startup. Replacing the per-HFML subprocess would save ~27s a run.

Read the timings before optimising anything else; the first round of caching was
aimed at what turned out to be 18 seconds of an 18-minute run.

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
`backend\`, `config_loader.py`, `docs\`, `requirements.txt`, `README.md`),
except `tests\`.
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

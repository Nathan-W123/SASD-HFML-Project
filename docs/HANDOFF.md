# Session Handoff

## What's been built

**Project structure**
```
frontend/       — Streamlit UI
backend/        — phase1–5 scripts
docs/           — CLAUDE.md, PIPELINE.md, this file
.streamlit/     — dark theme config (must stay at project root)
temp/           — runtime data passed between phases (gitignored)
config.json     — all shared paths and SQL connection details
config_loader.py — validates config.json on load
requirements.txt — pip dependencies
launch.bat      — Windows double-click launcher
```

**Frontend**
- `frontend/app.py` — dark-themed Streamlit UI: descriptive phase buttons, per-phase status badges, Run All at top (halts on failure), scrollable log panel, config validation warning on startup

**Backend — all phases wired into app via `run(log_fn)` signature**
- `backend/phase1/run.py` — full skeleton: SQL query → CSV → Excel `import` sheet
- `backend/phase2/run.py` — full skeleton: detect added MLs + frequency changes, append to mainlines, delete import sheet, write `temp/added_mls.json`
- `backend/phase3/run.py` — skeleton: arcpy ExcelToTable + Append upsert with field mapping
- `backend/phase4/run.py` — skeleton: loop over added HFMLs, recursive upstream ML trace, append MLs → select contacting LLs → append LLs → select intersecting parcels → append parcels
- `backend/phase5/run.py` — skeleton: aspect ratio template selection, arcpy.mp PDF export loop. Element/layer names stubbed with TODOs.

**Prototype scripts (reference only)**
- `backend/phase1/SASD HFML Querying GIS import.py` — original Phase 1 prototype
- `backend/phase4/SASD HFML Mainline Append.py` — original Phase 4 prototype

## How to run

**Mac (no ArcGIS):**
```
cd "/path/to/SASD"
python3 -m streamlit run frontend/app.py
```

**Windows work machine:**
Double-click `launch.bat` — no terminal needed.

**Install dependencies:**
```
pip install -r requirements.txt
```
Note: `arcpy` is not in requirements.txt — it comes from ArcGIS Pro's conda environment, handled by `launch.bat`.

## What needs to be done on the work machine

### Phase 2
- Open the Excel workbook and confirm `ML_ID_COL` and `FREQ_COL` values at the top of `backend/phase2/run.py`

### Phase 3
- Confirm field mapping names in `FIELD_MAPPINGS` match the ArcGIS feature class schema

### Phase 4
- Confirm `LL_SOURCE`, `LL_DEST`, `PARCELS_SOURCE`, `PARCELS_DEST` layer names in `backend/phase4/run.py`
- Verify `BOUNDARY_TOUCHES` is the correct spatial relationship for LLs contacting MLs

### Phase 5
- Open the `.aprx` templates and confirm:
  - `HFML_LAYER_NAME` — the layer to scope with a definition query
  - `HFML_ID_FIELD` — the field used in the definition query
  - `TEXT_ELEMENT_TITLE`, `TEXT_ELEMENT_DATE`, `TEXT_ELEMENT_ID` — dynamic text element names
  - Map frame element name (currently assumes index 0)

### config.json
- Verify all network drive paths are correct before first run
- `excel_workbook_path` is constant and does not change monthly

## Key decisions
- Each phase exposes `run(log_fn)` — output via callback, displayed in Streamlit log panel
- Phases 2 → 4/5 communicate via `temp/added_mls.json` (gitignored, created at runtime)
- All paths and credentials in `config.json`, never hardcoded
- `.streamlit/config.toml` must stay at project root — Streamlit requires it
- Removed MLs are intentionally ignored — only added MLs are appended

# Session Handoff

## What's been built

**Project structure**
- `frontend/` — Streamlit UI
- `backend/` — phase scripts (phase1–5)
- `docs/` — CLAUDE.md (ruleset), PIPELINE.md (workflow), this file
- `.streamlit/` — dark theme config (must stay at project root)

**Frontend**
- `frontend/app.py` — full Streamlit UI: dark theme, descriptive phase buttons, per-phase status badges (Not Run / Running / Complete / Failed), Run All at top (halts + highlights on failure), scrollable monospace log panel with Clear button

**Backend**
- `backend/phase1/run.py` — stub, wired into app
- `backend/phase2/run.py` — stub, wired into app
- `backend/phase3/run.py` — stub, wired into app
- `backend/phase4/run.py` — stub, wired into app
- `backend/phase5/run.py` — stub, wired into app
- `backend/phase1/SASD HFML Querying GIS import.py` — Phase 1 prototype (SQL → Excel)
- `backend/phase4/SASD HFML Mainline Append.py` — Phase 4 prototype (upstream network trace + append)

**Config**
- `config.json` — all shared network drive paths and SQL connection details. Excel workbook path is constant, does not change monthly. Update paths to match actual network drive locations before running.
- `config_loader.py` — loads and validates config.json on startup. All phases import this with:
    ```python
    from config_loader import load_config
    config = load_config()
    ```

**Launcher**
- `launch.bat` — Windows double-click launcher, activates ArcGIS Pro conda env and starts the app

## How to run

**Mac (no ArcGIS):**
```
cd "/path/to/SASD"
python3 -m streamlit run frontend/app.py
```

**Windows work machine:**
Double-click `launch.bat` — no terminal needed.

## What's blocked / next steps

- **Phase 1** — needs SQL Server access and the .sql file on the network drive. Prototype exists in `backend/phase1/` to build from.
- **Phase 2** — needs access to the real Excel workbook to confirm column names and sheet structure before writing openpyxl logic
- **Phase 3** — needs arcpy (ArcGIS Pro)
- **Phase 4** — needs arcpy. Prototype exists in `backend/phase4/` to build from.
- **Phase 5** — needs arcpy and the actual .aprx template files on the network drive

## Key decisions
- Each phase exposes a single `run(log_fn)` function — phase writes output via the callback, app displays it in the log panel
- All paths and credentials live in `config.json`, never hardcoded in scripts
- `.streamlit/config.toml` must stay at the project root — Streamlit requires it there

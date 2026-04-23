import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subprocess

import streamlit as st
from datetime import datetime

from backend.phase1.run import run as run_phase1
from backend.phase2.run import run as run_phase2
from backend.phase3.run import run as run_phase3
from backend.phase4.run import run as run_phase4
from backend.phase5.run import run as run_phase5
from config_loader import load_config

st.set_page_config(page_title="SASD HFML Pipeline", layout="centered")

st.markdown("""
<style>
    .block-container { padding-top: 2rem; max-width: 780px; }

    /* Left-align phase buttons */
    div[data-testid="stButton"] button {
        text-align: left !important;
        justify-content: flex-start !important;
        padding-left: 1rem;
        font-size: 0.95rem;
    }

    /* Status badge base */
    .badge {
        display: inline-block;
        padding: 4px 10px;
        border-radius: 20px;
        font-size: 0.78rem;
        font-weight: 600;
        white-space: nowrap;
        margin-top: 6px;
    }
    .badge-idle    { color: #6B7280; border: 1px solid #374151; }
    .badge-running { color: #F59E0B; border: 1px solid #92400E; animation: pulse 1.2s ease-in-out infinite; }
    .badge-complete{ color: #10B981; border: 1px solid #065F46; }
    .badge-failed  { color: #EF4444; border: 1px solid #7F1D1D; }

    @keyframes pulse {
        0%, 100% { opacity: 1; }
        50%       { opacity: 0.4; }
    }

    /* Log area monospace */
    .stTextArea textarea {
        font-family: 'Courier New', monospace !important;
        font-size: 0.82rem !important;
        background-color: #0D1117 !important;
        color: #8B949E !important;
        border: 1px solid #30363D !important;
    }

    /* Divider spacing */
    hr { margin: 1rem 0 !important; border-color: #21262D !important; }

    /* Phase row vertical alignment */
    .badge-cell {
        display: flex;
        align-items: center;
        height: 100%;
        padding-top: 4px;
    }
</style>
""", unsafe_allow_html=True)

PHASES = [
    ("phase1", "Import SQL query results into Excel",          run_phase1),
    ("phase2", "Detect ML changes and rebuild workbook",       run_phase2),
    ("phase3", "Sync Excel data to ArcGIS attribute table",    run_phase3),
    ("phase4", "Append upstream features for new HFMLs",       run_phase4),
    ("phase5", "Export PDF maps for new HFMLs",                run_phase5),
]

SUBPROCESS_PHASES = {"phase3", "phase4", "phase5"}

for key, _, _ in PHASES:
    if f"status_{key}" not in st.session_state:
        st.session_state[f"status_{key}"] = "idle"
if "log_lines" not in st.session_state:
    st.session_state.log_lines = []


def add_log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    st.session_state.log_lines.append(f"[{ts}]  {msg}")


def badge_html(status):
    labels = {
        "idle":     ("● Not Run",   "badge-idle"),
        "running":  ("● Running…",  "badge-running"),
        "complete": ("✓ Complete",  "badge-complete"),
        "failed":   ("✗ Failed",    "badge-failed"),
    }
    text, cls = labels.get(status, labels["idle"])
    return f'<div class="badge-cell"><span class="badge {cls}">{text}</span></div>'


def execute_phase_subprocess(key, log_fn, refresh_fn):
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    runner_path = os.path.join(project_root, "backend", "run_phase.py")
    proc = subprocess.Popen(
        [sys.executable, "-u", runner_path, key],
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    for line in proc.stdout:
        line = line.rstrip()
        if line:
            log_fn(line)
            refresh_fn()

    if proc.wait() != 0:
        raise RuntimeError(f"{key} failed in ArcGIS subprocess")


def execute_phase(key, label, runner, refresh_fn):
    st.session_state[f"status_{key}"] = "running"
    add_log(f"{label} — started")
    refresh_fn()
    try:
        if key in SUBPROCESS_PHASES:
            execute_phase_subprocess(key, add_log, refresh_fn)
        else:
            runner(add_log)
        st.session_state[f"status_{key}"] = "complete"
    except Exception as e:
        st.session_state[f"status_{key}"] = "failed"
        add_log(f"ERROR: {e}")
        raise


# ── Config check ──────────────────────────────────────────────────────────────
try:
    load_config()
    _config_ok = True
except Exception as _config_err:
    _config_ok = False

# ── Header ────────────────────────────────────────────────────────────────────
st.title("SASD HFML Pipeline")
st.caption("High Frequency Mainline — monthly data pipeline")

if not _config_ok:
    st.error(f"config.json error: {_config_err} — update config.json before running any phase.")

st.markdown("---")

# ── Run All ───────────────────────────────────────────────────────────────────
_run_all = st.button("▶  Run All Phases", use_container_width=True, type="primary")

st.markdown("---")

# ── Log panel — created before phase buttons so placeholder is live during execution ──
col_title, col_clear = st.columns([5, 1])
with col_title:
    st.subheader("Log Output")
with col_clear:
    st.markdown("<div style='margin-top:8px'>", unsafe_allow_html=True)
    if st.button("Clear", use_container_width=True):
        st.session_state.log_lines = []
        st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

_log_placeholder = st.empty()


def _render_log():
    log_text = "\n".join(st.session_state.log_lines) if st.session_state.log_lines else "No output yet."
    _log_placeholder.code(log_text, language=None)


_render_log()

st.markdown("---")

# ── Phase rows ────────────────────────────────────────────────────────────────
for key, label, runner in PHASES:
    status = st.session_state[f"status_{key}"]
    col_btn, col_badge = st.columns([5, 1])
    with col_btn:
        if st.button(label, key=f"btn_{key}", use_container_width=True):
            try:
                execute_phase(key, label, runner, _render_log)
            except Exception:
                pass
            st.rerun()
    with col_badge:
        st.markdown(badge_html(status), unsafe_allow_html=True)

# ── Run All execution (after placeholders are created) ────────────────────────
if _run_all:
    for key, label, runner in PHASES:
        try:
            execute_phase(key, label, runner, _render_log)
        except Exception:
            add_log("Pipeline halted.")
            break
    else:
        add_log("Pipeline complete.")
    st.rerun()

import importlib
import os
import subprocess
import sys

import arcpy

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PHASES = [
    ("backend.phase1.run", "Import SQL query results into Excel"),
    ("backend.phase2.run", "Detect ML changes and rebuild workbook"),
    ("backend.phase3.run", "Sync Excel data to ArcGIS attribute table"),
    ("backend.phase4.run", "Append upstream features for new HFMLs"),
    ("backend.phase5.run", "Export PDF maps for new HFMLs"),
]

# arcpy.mp layout export fails with "General Function Failure" when run inside
# the ArcGIS Pro process, so these phases run in a standalone Python process.
OUT_OF_PROCESS = {"backend.phase5.run"}


def _python_exe():
    # Inside Pro, sys.executable is ArcGISPro.exe; the active env's python.exe lives in sys.exec_prefix.
    exe = os.path.join(sys.exec_prefix, "python.exe")
    if not os.path.exists(exe):
        raise RuntimeError(f"Could not find ArcGIS Pro's python.exe at {exe}")
    return exe


def _run_out_of_process(module_name):
    code = (
        "import importlib; "
        f"importlib.import_module('{module_name}').run(lambda m: print(m, flush=True))"
    )
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(
        [_python_exe(), "-u", "-c", code],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for line in proc.stdout:
        line = line.rstrip()
        if line:
            arcpy.AddMessage(line)
    if proc.wait() != 0:
        raise RuntimeError(f"{module_name} exited with code {proc.returncode}")


class Toolbox:
    def __init__(self):
        self.label = "SASD HFML Pipeline"
        self.alias = "sasdhfml"
        self.tools = [RunPipeline]


class RunPipeline:
    def __init__(self):
        self.label = "Run HFML Pipeline"
        self.description = "Runs phases 1-5 of the monthly HFML pipeline in order, halting on the first failure."
        self.canRunInBackground = True

    def getParameterInfo(self):
        return []

    def isLicensed(self):
        return True

    def execute(self, parameters, messages):
        # Pro keeps modules cached between runs; reload so code edits take effect without restarting Pro.
        importlib.reload(importlib.import_module("config_loader"))
        for i, (module_name, label) in enumerate(PHASES, 1):
            arcpy.SetProgressorLabel(f"Phase {i}: {label}")
            arcpy.AddMessage(f"=== Phase {i}: {label} ===")
            try:
                if module_name in OUT_OF_PROCESS:
                    _run_out_of_process(module_name)
                else:
                    module = importlib.reload(importlib.import_module(module_name))
                    module.run(arcpy.AddMessage)
            except Exception as exc:
                arcpy.AddError(f"Phase {i} failed: {exc}")
                arcpy.AddError("Pipeline halted.")
                raise
            arcpy.AddMessage(f"Phase {i} complete.")
        arcpy.AddMessage("Pipeline complete.")

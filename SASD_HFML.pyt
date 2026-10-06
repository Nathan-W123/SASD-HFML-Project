import importlib
import os
import sys

import arcpy

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PHASES = [
    ("backend.phase1.run", "Import SQL query results into Excel"),
    ("backend.phase2.run", "Detect ML changes and rebuild workbook"),
    ("backend.phase3.run", "Sync Excel data to ArcGIS attribute table"),
    ("backend.phase4.run", "Trace and map the next 5 new HFMLs"),
    ("backend.phase5.run", "Verify PDF maps from Phase 4"),
]

# Map export runs in a standalone Python process (see
# export_hfml_map_out_of_process in backend/phase5/run.py), launched per HFML
# from Phase 4, because arcpy.mp layout export fails inside the Pro process.


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
                module = importlib.reload(importlib.import_module(module_name))
                module.run(arcpy.AddMessage)
            except Exception as exc:
                arcpy.AddError(f"Phase {i} failed: {exc}")
                arcpy.AddError("Pipeline halted.")
                raise
            arcpy.AddMessage(f"Phase {i} complete.")
        arcpy.AddMessage("Pipeline complete.")

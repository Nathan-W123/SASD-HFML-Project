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


def _phase_choice(i, label):
    return f"Phase {i}: {label}"


PHASE_CHOICES = [_phase_choice(i, label) for i, (_, label) in enumerate(PHASES, 1)]


class RunPipeline:
    def __init__(self):
        self.label = "Run HFML Pipeline"
        self.description = (
            "Runs the selected phases of the monthly HFML pipeline in order (all five by default), "
            "halting on the first failure."
        )
        self.canRunInBackground = True

    def getParameterInfo(self):
        phases = arcpy.Parameter(
            displayName="Phases to run",
            name="phases",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
            multiValue=True,
        )
        phases.filter.type = "ValueList"
        phases.filter.list = PHASE_CHOICES
        phases.values = PHASE_CHOICES
        return [phases]

    def isLicensed(self):
        return True

    def execute(self, parameters, messages):
        selected = set(parameters[0].values or [])
        # Pro keeps modules cached between runs; reload so code edits take effect without restarting Pro.
        # Reload every phase (not just the selected ones) since Phase 4 imports helpers from Phase 5.
        importlib.reload(importlib.import_module("config_loader"))
        modules = [importlib.reload(importlib.import_module(name)) for name, _ in PHASES]
        for i, ((_, label), module) in enumerate(zip(PHASES, modules), 1):
            if _phase_choice(i, label) not in selected:
                continue
            arcpy.SetProgressorLabel(f"Phase {i}: {label}")
            arcpy.AddMessage(f"=== Phase {i}: {label} ===")
            try:
                module.run(arcpy.AddMessage)
            except Exception as exc:
                arcpy.AddError(f"Phase {i} failed: {exc}")
                arcpy.AddError("Pipeline halted.")
                raise
            arcpy.AddMessage(f"Phase {i} complete.")
        arcpy.AddMessage("Pipeline complete.")

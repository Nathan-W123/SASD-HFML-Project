import sys, types, time as _time
# P4.time is the shared time module, so patching its sleep below silences
# sleep everywhere.  Keep the real one for the stopwatch check.
REAL_SLEEP = _time.sleep
import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
sys.modules.setdefault("pyodbc", types.ModuleType("pyodbc"))
import backend.phase4.run as P4
import backend.phase5.run as P5

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond: fails.append(name)

P4.time.sleep = lambda s: None          # retries back off with sleep; skip it here
P4._append_by_oids = lambda *a, **k: None
P4._get_max_oid = lambda *a, **k: 0

class F:
    def __init__(self, name, type_="String", editable=True):
        self.name, self.type, self.editable = name, type_, editable

class Cur:
    def __init__(self, rows): self.rows = rows
    def __iter__(self): return iter(self.rows)
    def __enter__(self): return iter(self.rows)
    def __exit__(self, *a): return False

class UCur:
    def __init__(self, store): self.store = store
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self): return iter([[10]])
    def updateRow(self, row): self.store.append(row)

def make_arcpy(fail_reads):
    state = {"reads": 0, "updated": []}
    a = types.SimpleNamespace()
    hfml_fields = [F("OBJECTID", "OID"), F("PM_Mainlines_pmnum"), F("PM_Mainlines_frequency", "Double")]
    pm_fields = [F("OBJECTID", "OID"), F("pmnum"), F("frequency", "Double")]
    def ListFields(path):
        if path == "PM":
            state["reads"] += 1
            if state["reads"] <= fail_reads:
                raise OSError("PM_Mainlines does not exist")
            return pm_fields
        return hfml_fields
    a.ListFields = ListFields
    def SearchCursor(path, fields, where=None):
        if path == "PM":
            return Cur([("32536", 6.0)])
        return Cur([])
    a.da = types.SimpleNamespace(SearchCursor=SearchCursor,
                                 UpdateCursor=lambda *a_, **k: UCur(state["updated"]))
    a.management = types.SimpleNamespace(ClearWorkspaceCache=lambda *a_, **k: None)
    a._state = state
    return a

# transient failure (like the one in the 2026-11-17 log) recovers on retry
a = make_arcpy(fail_reads=1)
lines = []
P4._append_seed_to_hfml_layer(a, [48967], "ml", "HFML", "32536", "PM", lines.append)
check("stale read recovers on retry", any("PM_Mainlines_frequency=6.0" in l for l in lines), lines)
check("attributes were copied", a._state["updated"] and 6.0 in a._state["updated"][0], a._state["updated"])

# persistent failure fails the HFML instead of mapping it without attributes
a = make_arcpy(fail_reads=99)
raised = None
try:
    P4._append_seed_to_hfml_layer(a, [48967], "ml", "HFML", "32536", "PM", lambda m: None)
except RuntimeError as exc:
    raised = str(exc)
check("persistent failure raises", raised and "Reading PM_Mainlines attributes for pmnum 32536" in raised, raised)
check("nothing stamped on failure", a._state["updated"] == [], a._state["updated"])

# the generic retry keeps its old schedule; the PM_Mainlines read uses the longer one
waits = []
P4.time.sleep = waits.append
calls = {"n": 0}
def always_fails():
    calls["n"] += 1
    raise OSError("locked")
try:
    P4._with_arcpy_retry(types.SimpleNamespace(management=types.SimpleNamespace(
        ClearWorkspaceCache=lambda *a: None)), always_fails, "Appending")
except RuntimeError as exc:
    msg = str(exc)
check("default retry: three attempts, 1s then 2s", calls["n"] == 3 and waits == [1, 2], (calls, waits))
check("default retry message unchanged", "failed after 3 attempts" in msg, msg)

waits.clear()
a = make_arcpy(fail_reads=99)
try:
    P4._append_seed_to_hfml_layer(a, [48967], "ml", "HFML", "32536", "PM", lambda m: None)
except RuntimeError:
    pass
check("PM_Mainlines read waits 2s, 5s, 10s", waits == [2, 5, 10], waits)
P4.time.sleep = lambda s: None

# ---- export stopwatch ------------------------------------------------------
w = P5._Stopwatch()
with w.part("fast"):
    pass
with w.part("slow"):
    REAL_SLEEP(0.02)
check("slowest part listed first", w.summary().index("slow") < w.summary().index("fast"), w.summary())

# a failing export still reports the timing it gathered
JOB = {"pmnum": "1", "pdf_path": "x.pdf", "draft_aprx_path": "x.aprx"}
def boom(*a, **k): raise RuntimeError("extent failed")
real_info, real_row = P5._get_phase4_feature_info, P5._read_pm_row
P5._read_pm_row = lambda *a, **k: {}
P5._get_phase4_feature_info = boom
lines = []
try:
    P5.export_hfml_map(types.SimpleNamespace(), JOB, {}, "today", {}, lines.append)
except RuntimeError:
    pass
check("failed export still logs its timing",
      any(l.startswith("  Inside the export:") and "feature extents" in l for l in lines), lines)

# so does one that fails reading PM_Mainlines, which now happens first
def unreadable(*a, **k): raise RuntimeError("Reading PM_Mainlines for pmnum 1 failed after 4 attempts")
P5._read_pm_row = unreadable
lines = []
raised = False
try:
    P5.export_hfml_map(types.SimpleNamespace(), JOB, {}, "today", {}, lines.append)
except RuntimeError:
    raised = True
check("unreadable PM_Mainlines fails the map", raised)
check("and still logs its timing",
      any(l.startswith("  Inside the export:") and "PM_Mainlines row" in l for l in lines), lines)
P5._get_phase4_feature_info, P5._read_pm_row = real_info, real_row

print()
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)

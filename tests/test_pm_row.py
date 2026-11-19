import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
import datetime, random, types
sys.modules.setdefault("pyodbc", types.ModuleType("pyodbc"))
import stub_arcpy as S
import backend.phase5.run as P5
from baseline import load_baseline, EXPORT_BASELINE_COMMIT

fails = []
def check(name, cond, detail=""):
    if not cond:
        print("FAIL " + name + " :: " + str(detail)); fails.append(name)
    return cond

F = S.Field
SCHEMAS = {
    "PM": [F("OBJECTID", "OID", False), F("Shape", "Geometry"), F("pmnum"), F("frequency", "Double"),
           F("laststartdate", "Date"), F("assetnum"), F("gridno"), F("description")],
    "ML": [F("OBJECTID", "OID", False), F("Shape", "Geometry"), F("MXASSETNUM"), F("DIAMETER", "Double"),
           F("MATLTYPE"), F("GRID_NO")],
}
CONFIG = {"feature_class_path": "PM", "p4_ml_source_path": "ML"}
geom = S.Geom("point", S.Pt(0, 0))

def world(seed):
    rnd = random.Random(seed)
    pm, ml = {}, {}
    for oid in range(1, 25):
        asset = rnd.choice([f"A{oid}", f"A{oid}", None, "NOT-IN-ML"])
        pm[oid] = {"SHAPE": geom, "pmnum": str(1000 + oid),
                   "frequency": rnd.choice([6.0, 3.0, 1.0, 12, None]),
                   "laststartdate": rnd.choice([datetime.datetime(2026, 3, rnd.randint(1, 28)), None]),
                   "assetnum": asset,
                   "gridno": rnd.choice([f"3{oid:02d}-15{oid % 9}-20{oid:02d}", None]),
                   "description": "x"}
        if asset and asset.startswith("A"):
            ml[oid] = {"SHAPE": geom, "MXASSETNUM": asset, "DIAMETER": rnd.choice([6.0, 8.0, None]),
                       "MATLTYPE": rnd.choice(["VCP", "PVC", None]),
                       "GRID_NO": rnd.choice([f"G{oid}", None])}
    return {"PM": pm, "ML": ml}

# ---- 1. same figure label and callout text as before, whenever the read works --
OLD = load_baseline("backend/phase5/run.py", EXPORT_BASELINE_COMMIT)
if OLD is None:
    print("SKIP part 1: needs git history for the pre-change export (see baseline.py)")
else:
    compared = 0
    for seed in range(40):
        data = world(seed)
        ids = [r["pmnum"] for r in data["PM"].values()] + ["9999"]   # 9999: not in the table
        for hfml_id in ids:
            obs = random.Random(seed).choice(["", "Grease"])
            a_old, a_new = S.Arcpy(data, SCHEMAS), S.Arcpy(data, SCHEMAS)
            old_grid = OLD._lookup_gridno(a_old, CONFIG, hfml_id)
            old_text = OLD._get_callout_text(a_old, CONFIG, hfml_id, obs, None, gridno=old_grid)
            row = P5._read_pm_row(a_new, CONFIG, hfml_id)
            new_grid = P5._lookup_gridno(a_new, CONFIG, hfml_id, pm_row=row)
            new_text = P5._get_callout_text(a_new, CONFIG, hfml_id, obs, None, gridno=new_grid, pm_row=row)
            check(f"seed {seed} pm {hfml_id}: same figure label", new_grid == old_grid, (new_grid, old_grid))
            check(f"seed {seed} pm {hfml_id}: same callout text", new_text == old_text, (new_text, old_text))
            compared += 1
    print(f"figure label and callout text identical to the previous export for {compared} PMs")

# ---- 2. one PM_Mainlines read per map, not several -----------------------------
data = world(1)
a = S.Arcpy(data, SCHEMAS)
reads = []
real_search = a._search
a.da.SearchCursor = lambda t, *x, **k: (reads.append(t), real_search(t, *x, **k))[1]
row = P5._read_pm_row(a, CONFIG, "1001")
P5._lookup_gridno(a, CONFIG, "1001", pm_row=row)
P5._get_callout_text(a, CONFIG, "1001", "", None, pm_row=row)
check("PM_Mainlines read once", reads.count("PM") == 1, reads)

# ---- 3. a transient failure is retried; a lasting one fails the map -------------
waits = []
P5.time.sleep = waits.append
def flaky(fail_times):
    a = S.Arcpy(world(2), SCHEMAS)
    real = a.ListFields
    count = {"n": 0}
    def ListFields(path):
        if path == "PM" and count["n"] < fail_times:
            count["n"] += 1
            raise OSError(r"\\sdfiles\...\Data.gdb\PM_Mainlines does not exist")
        return real(path)
    a.ListFields = ListFields
    return a

lines = []
flaky_arcpy = flaky(2)
row = P5._read_pm_row(flaky_arcpy, CONFIG, "1001", lines.append)
check("recovers after two failed reads", row.get("pmnum") == "1001", row)
check("workspace cache cleared before each retry", flaky_arcpy.calls.get("ClearWorkspaceCache") == 2, flaky_arcpy.calls)
check("retries logged", sum("retrying in" in l for l in lines) == 2, lines)
check("waits back off", waits == [2, 5], waits)

waits.clear(); lines.clear()
raised = None
try:
    P5._read_pm_row(flaky(99), CONFIG, "1001", lines.append)
except RuntimeError as exc:
    raised = str(exc)
check("lasting failure fails the map", raised and "failed after 4 attempts" in raised and "does not exist" in raised, raised)
check("all waits used before giving up", waits == [2, 5, 10], waits)

# the figure label no longer falls back silently when the table cannot be read
waits.clear()
raised = False
try:
    P5._lookup_gridno(flaky(99), CONFIG, "1001")
except RuntimeError:
    raised = True
check("figure label lookup no longer hides a read failure", raised)

# a pmnum genuinely not in the table still falls back, as before
a = S.Arcpy(world(3), SCHEMAS)
row = P5._read_pm_row(a, CONFIG, "424242")
check("unknown pmnum gives an empty row", row == {}, row)
check("and the figure label falls back to the HFML number", P5._lookup_gridno(a, CONFIG, "424242", pm_row=row) == "424242")

print()
print("FAILURES: " + (", ".join(fails[:10]) if fails else "none"))
sys.exit(1 if fails else 0)

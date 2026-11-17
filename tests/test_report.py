import sys, types
import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
# phase5 imports pyodbc at module level; only ArcGIS Pro has it.
sys.modules.setdefault("pyodbc", types.ModuleType("pyodbc"))
from backend.phase5.run import _log_layer_report, _report_offscreen_features, _extent_overlaps

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond: fails.append(name)

CONFIG = {
    "p4_ml_layer_path": r"C:\gdb\Data.gdb\PMs_SSGRAVITYMAIN",
    "p4_ml_dest_path": r"C:\gdb\Data.gdb\Trace_SSGRAVITYMAIN",
    "p4_ll_dest_path": r"C:\gdb\Data.gdb\Lower_Laterals",
    "p4_parcels_dest_path": r"C:\gdb\Data.gdb\Parcels_trace",
    "p4_ml_source_path": r"C:\gdb\src.gdb\ml",
    "p4_ll_source_path": r"C:\gdb\src.gdb\ll",
    "p4_parcels_source_path": r"C:\gdb\src.gdb\parcels",
}

class Layer:
    def __init__(self, name, source, count, group=False):
        self.name, self.dataSource, self._count = name, source, count
        self.isGroupLayer, self.definitionQuery = group, ""
        self.symbology = types.SimpleNamespace(
            renderer=types.SimpleNamespace(type="SimpleRenderer", fields=None))
    def supports(self, what): return True

class MapObj:
    name = "Map"
    def __init__(self, layers): self._layers = layers
    def listLayers(self): return self._layers

class Cur:
    # The real code iterates the cursor directly (sum(1 for _ in ...)),
    # so the stub has to be iterable, not only a context manager.
    def __init__(self, n): self.n = n
    def __iter__(self): return iter([(i,) for i in range(self.n)])
    def __enter__(self): return iter(self)
    def __exit__(self, *a): return False

class FakeArcpy:
    def __init__(self, geoms=None):
        self.geoms = geoms or {}
        self.da = types.SimpleNamespace(SearchCursor=self._search)
    def _search(self, target, fields, where=None):
        if isinstance(target, Layer):
            return Cur(target._count)
        rows = [(oid, geom) for oid, geom in self.geoms.items()
                if where is None or str(oid) in where]
        class C:
            def __enter__(self_inner): return iter(rows)
            def __exit__(self_inner, *a): return False
        return C()

# Five layers, like the real project. The old bug blew up on the second one.
layers = [
    Layer("Trace_SSGRAVITYMAIN", CONFIG["p4_ml_dest_path"], 23),
    Layer("Lower_Laterals", CONFIG["p4_ll_dest_path"], 80),
    Layer("High Freq ML PMs", CONFIG["p4_ml_layer_path"], 1),
    Layer("Trace Upstream High Freq PM", CONFIG["p4_ml_dest_path"], 22),
    Layer("Parcels in the Trace", CONFIG["p4_parcels_dest_path"], 73),
    Layer("Basemap", r"C:\other\basemap", 1),
]
hfml = {"pmnum": "7780", "seed_oid": 47187, "seed_oids": [47187],
        "ml_oids": list(range(23)), "ll_oids": list(range(80)),
        "parcel_oids": list(range(73))}

lines = []
_log_layer_report(FakeArcpy(), MapObj(layers), "7780", CONFIG, lines.append, hfml=hfml)
check("walks every layer without crashing", len(lines) >= 5, lines)
check("reports each role", sum("draws" in l for l in lines) >= 5, lines)
check("ML trace matching appended -> no warning",
      not any("Trace_SSGRAVITYMAIN'" in l and "WARNING" in l for l in lines), lines)
check("22 drawn of 23 appended -> flags the shortfall",
      any("Trace Upstream" in l and "1 of the 23 appended this run are not drawn" in l
          for l in lines), lines)

# more drawn than appended = rows left by an earlier run
layers[0]._count = 35
lines = []
_log_layer_report(FakeArcpy(), MapObj(layers), "7780", CONFIG, lines.append, hfml=hfml)
check("35 drawn of 23 appended -> flags stale rows",
      any("12 more than the 23 appended" in l and "earlier run" in l for l in lines), lines)

# no record passed -> old behaviour, no warnings, still no crash
lines = []
_log_layer_report(FakeArcpy(), MapObj(layers), "7780", CONFIG, lines.append)
check("hfml=None still reports", any("draws" in l for l in lines), lines)
check("hfml=None adds no warnings", not any("WARNING: " in l and "appended" in l for l in lines), lines)

# zero appended everywhere must not divide-by-anything or crash
lines = []
_log_layer_report(FakeArcpy(), MapObj(layers), "x", CONFIG,
                  lines.append, hfml={"pmnum": "x"})
check("empty record is handled", any("draws" in l for l in lines), lines)

# ---- offscreen report ----------------------------------------------------
class Ext:
    def __init__(self, x1, y1, x2, y2):
        self.XMin, self.YMin, self.XMax, self.YMax = x1, y1, x2, y2
class Geom:
    def __init__(self, ext): self.extent = ext

camera = Ext(0, 0, 100, 100)
check("overlap test: inside", _extent_overlaps(Ext(10, 10, 20, 20), camera))
check("overlap test: outside", not _extent_overlaps(Ext(200, 200, 210, 210), camera))
check("overlap test: touching edge", _extent_overlaps(Ext(100, 50, 110, 60), camera))

arcpy = FakeArcpy(geoms={1: Geom(Ext(10, 10, 20, 20)),
                         2: Geom(Ext(500, 500, 510, 510)),
                         3: None})
lines = []
_report_offscreen_features(
    arcpy, {"ll_oids": [1, 2, 3]}, CONFIG, camera, lines.append)
check("names the offscreen feature", any("outside the map view" in l and "[2]" in l for l in lines), lines)
check("names the feature with no geometry",
      any("no geometry" in l and "[3]" in l for l in lines), lines)
check("says nothing about the visible one",
      not any("[1]" in l for l in lines), lines)

lines = []
_report_offscreen_features(arcpy, {"ll_oids": [1]}, CONFIG, camera, lines.append)
check("silent when everything is on screen", lines == [], lines)

lines = []
_report_offscreen_features(arcpy, {}, CONFIG, camera, lines.append)
check("silent with nothing appended", lines == [], lines)

print()
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)

import sys, types
import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
sys.modules.setdefault("pyodbc", types.ModuleType("pyodbc"))
import backend.phase5.run as P5

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond: fails.append(name)

class Layer:
    def __init__(self, name, selection=None, group=False, feature=True, raises=False, query=""):
        self.name, self._sel, self.isGroupLayer, self.isFeatureLayer = name, selection, group, feature
        self._raises, self.definitionQuery, self.dataSource = raises, query, "src"
    def getSelectionSet(self):
        if self._raises: raise RuntimeError("not supported")
        return self._sel
    def supports(self, what): return True

class Map:
    def __init__(self, layers): self._layers = layers
    def listLayers(self): return self._layers

calls = []
arcpy = types.SimpleNamespace(management=types.SimpleNamespace(
    SelectLayerByAttribute=lambda lyr, kind, *a: calls.append((lyr.name, kind))))

m = Map([
    Layer("no selection (normal)", selection=set()),
    Layer("None selection", selection=None),
    Layer("has selection", selection={3, 4}),
    Layer("group", group=True, selection={1}),
    Layer("basemap", feature=False, selection={1}),
    Layer("cannot tell", raises=True),
])
n = P5._clear_layer_selections(arcpy, m)
names = [c[0] for c in calls]
check("clears a layer that has a selection", "has selection" in names, names)
check("falls back to clearing when a layer cannot report", "cannot tell" in names, names)
check("skips layers with nothing selected", not {"no selection (normal)", "None selection"} & set(names), names)
check("skips group and non-feature layers", not {"group", "basemap"} & set(names), names)
check("returns the layers it cleared", sorted(n) == ["cannot tell", "has selection"], n)

# 20 layers, none selected: zero geoprocessing calls (was 20)
calls.clear()
P5._clear_layer_selections(arcpy, Map([Layer(f"L{i}", selection=set()) for i in range(20)]))
check("no tool calls when nothing is selected", calls == [], calls)

# ---- saved definition queries are reported --------------------------------
class Cur:
    def __init__(self, rows): self.rows = rows
    def __iter__(self): return iter(self.rows)
lines = []
scope_arcpy = types.SimpleNamespace(
    Describe=lambda src: types.SimpleNamespace(spatialReference="2226"),
    Polygon=lambda arr, sr: "poly", Array=lambda pts: pts, Point=lambda x, y: (x, y),
    management=types.SimpleNamespace(SelectLayerByLocation=lambda *a, **k: None,
                                     SelectLayerByAttribute=lambda *a, **k: None),
    da=types.SimpleNamespace(SearchCursor=lambda lyr, f, *a: Cur([(1,), (2,)])),
)
ext = types.SimpleNamespace(XMin=0, YMin=0, XMax=10, YMax=10)
filtered = Layer("Lower_Laterals", query="LIFECYCLESTATUS <> 'ABANDONED'")
plain = Layer("Parcels in the Trace", query="")
P5._scope_layers(scope_arcpy, Map([filtered, plain]), "1", ext, lines.append)
check("reports a saved definition query",
      any("Lower_Laterals" in l and "LIFECYCLESTATUS <> 'ABANDONED'" in l for l in lines), lines)
check("no saved-query line for a layer without one",
      not any("Parcels in the Trace" in l and "definition query" in l for l in lines), lines)
check("scoping still applied", filtered.definitionQuery == "OBJECTID IN (1,2)", filtered.definitionQuery)

print()
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)

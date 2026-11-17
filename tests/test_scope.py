import random, sys, types
import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
sys.modules.setdefault("pyodbc", types.ModuleType("pyodbc"))
import stub_arcpy as S
import backend.phase5.run as P5

fails = []
def check(name, cond, detail=""):
    if not cond:
        print("FAIL " + name + " :: " + str(detail)); fails.append(name)

class MapLayer:
    def __init__(self, name, source, query=""):
        self.name, self.dataSource, self.definitionQuery = name, source, query
        self.isGroupLayer = False
    def supports(self, what): return True

class Map:
    def __init__(self, layers): self._layers = layers
    def listLayers(self): return self._layers

def world(seed):
    rnd = random.Random(seed)
    laterals, parcels = {}, {}
    for oid in range(1, 120):
        x, y = rnd.uniform(-500, 1500), rnd.uniform(-500, 1500)
        ang = rnd.uniform(0, 6.28)
        L = rnd.uniform(20, 300)
        import math
        laterals[oid] = {"SHAPE": S.Geom("line", [S.Pt(x, y), S.Pt(x + L * math.cos(ang), y + L * math.sin(ang))]),
                         "STATUS": rnd.choice(["OPERATING", "OPERATING", "TOBEMOD", "ABANDONED"])}
        parcels[oid] = {"SHAPE": S.Geom("rect", (x, y, x + rnd.uniform(20, 80), y + rnd.uniform(20, 80)))}
    return {"Lower_Laterals": laterals, "Parcels_trace": parcels}

QUERY = "STATUS in( 'OPERATING', 'TOBEMOD')"
total = 0
for seed in range(200):
    data = world(seed)
    a = S.Arcpy(data)
    rnd = random.Random(seed)
    x1, y1 = rnd.uniform(-200, 600), rnd.uniform(-200, 600)
    ext = types.SimpleNamespace(XMin=x1, YMin=y1, XMax=x1 + rnd.uniform(100, 900), YMax=y1 + rnd.uniform(100, 900))
    view = S.Geom("rect", (ext.XMin, ext.YMin, ext.XMax, ext.YMax))
    lat = MapLayer("Lower_Laterals", "Lower_Laterals", QUERY)
    par = MapLayer("Parcels in the Trace", "Parcels_trace", "")
    P5._scope_layers(a, Map([lat, par]), "1", ext, lambda m: None)

    # the specification, computed independently
    def expected(ds, query):
        return sorted(o for o, r in data[ds].items()
                      if (not query or S._matches(query, o, r)) and r["SHAPE"].distanceTo(view) == 0)
    for layer, ds, q in ((lat, "Lower_Laterals", QUERY), (par, "Parcels_trace", "")):
        want = expected(ds, q)
        got = layer.definitionQuery
        want_q = f"OBJECTID IN ({','.join(map(str, want))})" if want else "1=0"
        check(f"seed {seed} {layer.name}: in-view features", got == want_q, (got[:80], want_q[:80]))
        total += len(want)
    check(f"seed {seed}: no selection tools used",
          not a.calls.get("SelectLayerByLocation") and not a.calls.get("SelectLayerByAttribute"), a.calls)
print(f"map-view filter: {total} in-view features across 200 views, matched the direct calculation")

# a layer the Python path cannot read falls back to the selection tools
calls = []
def broken_cursor(*a, **k): raise RuntimeError("cannot open dataset")
fake = types.SimpleNamespace(
    Describe=lambda src: types.SimpleNamespace(spatialReference="2226"),
    Polygon=lambda arr, sr: S.Polygon(arr, sr), Array=S.Array, Point=S.Point,
    da=types.SimpleNamespace(SearchCursor=lambda target, *a, **k:
        broken_cursor() if isinstance(target, str) else iter([(7,), (9,)])),
    management=types.SimpleNamespace(
        SelectLayerByLocation=lambda *a, **k: calls.append("select"),
        SelectLayerByAttribute=lambda *a, **k: calls.append("clear")),
)
lyr = MapLayer("Lower_Laterals", "somewhere")
lines = []
P5._scope_layers(fake, Map([lyr]), "1",
                 types.SimpleNamespace(XMin=0, YMin=0, XMax=10, YMax=10), lines.append)
check("fallback used the tools", calls == ["select", "clear"], calls)
check("fallback result applied", lyr.definitionQuery == "OBJECTID IN (7,9)", lyr.definitionQuery)
check("fallback reported", any("using the selection tools" in l for l in lines), lines)

print()
print("FAILURES: " + (", ".join(fails[:10]) if fails else "none"))
sys.exit(1 if fails else 0)

import shutil, sys, types
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
    def __init__(self, name, selection=None, group=False, feature=True, raises=False, query="",
                 direct="works"):
        self.name, self._sel, self.isGroupLayer, self.isFeatureLayer = name, selection, group, feature
        self._raises, self.definitionQuery, self.dataSource = raises, query, "src"
        self._direct = direct   # how setSelectionSet behaves: works | raises | ignored
    def getSelectionSet(self):
        if self._raises: raise RuntimeError("not supported")
        return self._sel
    def setSelectionSet(self, oids=None, method="NEW"):
        if self._direct == "raises": raise ValueError("oidList is empty")
        if self._direct == "works": self._sel = set(oids or ())
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
check("clears a layer that has a selection", "has selection" in n, n)
check("falls back to clearing when a layer cannot report", "cannot tell" in names, names)
check("skips layers with nothing selected", not {"no selection (normal)", "None selection"} & set(names), names)
check("skips group and non-feature layers", not {"group", "basemap"} & set(names), names)
check("returns the layers it cleared", sorted(n) == ["cannot tell (with the selection tool)", "has selection"], n)
check("cleared directly, no tool call for it", ("has selection", "CLEAR_SELECTION") not in calls, calls)
check("selection really empty afterwards", not m._layers[2].getSelectionSet(), m._layers[2]._sel)

# setSelectionSet raising, or returning without clearing: the tool runs instead
calls.clear()
raising = Layer("raises", selection={1}, direct="raises")
ignoring = Layer("ignored", selection={1}, direct="ignored")
n = P5._clear_layer_selections(arcpy, Map([raising, ignoring]))
names = [c[0] for c in calls]
check("tool used when the direct clear raises", "raises" in names, names)
check("tool used when the direct clear did not take", "ignored" in names, names)
check("fallbacks reported as such", sorted(n) == ["ignored (with the selection tool)", "raises (with the selection tool)"], n)

# 20 layers, none selected: zero geoprocessing calls (was 20)
calls.clear()
P5._clear_layer_selections(arcpy, Map([Layer(f"L{i}", selection=set()) for i in range(20)]))
check("no tool calls when nothing is selected", calls == [], calls)

# ---- the project's layers are checked once per run --------------------------
import tempfile, time as _t
class Timed(Layer):
    checked = []
    def __init__(self, *a, delay=0.0, broken=False, **k):
        super().__init__(*a, **k); self._delay, self.isBroken = delay, broken
    def getSelectionSet(self):
        Timed.checked.append(self.name)
        if self._delay: _t.sleep(self._delay)
        return super().getSelectionSet()

def project(selected=("Spill",)):
    """A fresh copy of the same template: same layers, same saved selections."""
    names = ["Spill", "High Freq ML PMs", "Parcels", "Laterals", "Mains", "Basemap roads"]
    return Map([Timed(n, selection=({7} if n in selected else set()),
                      delay=0.6 if n == "Basemap roads" else 0.0, broken=(n == "Basemap roads"))
                for n in names])

tmpdir = tempfile.mkdtemp()
template = os.path.join(tmpdir, "TEST.aprx")
with open(template, "w") as f:
    f.write("v1")
P5._TEMPLATE_SELECTIONS.clear()

lines = []
Timed.checked = []
first = P5._clear_layer_selections(arcpy, project(), template=template, log_fn=lines.append)
# (a cleared layer is asked twice: once to check, once to confirm the clear took)
check("first map checks every layer", len(set(Timed.checked)) == 6, Timed.checked)
check("first map clears the saved selection", first == ["Spill"], first)
check("scan summary logged", any("Checked 6 layer(s)" in l and "1 had one" in l for l in lines), lines)
check("slow and broken layer named",
      any("Slowest layers to check" in l and "Basemap roads" in l and "broken data source" in l for l in lines), lines)

lines = []
Timed.checked = []
second = P5._clear_layer_selections(arcpy, project(), template=template, log_fn=lines.append)
check("later maps check only the layer that had a selection", set(Timed.checked) == {"Spill"}, Timed.checked)
check("and still clear it", second == ["Spill"], second)
check("no scan logged for later maps", not any("Checked" in l for l in lines), lines)

# a project with nothing selected: later maps check nothing at all
P5._TEMPLATE_SELECTIONS.clear()
P5._clear_layer_selections(arcpy, project(selected=()), template=template)
Timed.checked = []
P5._clear_layer_selections(arcpy, project(selected=()), template=template)
check("nothing selected: later maps check no layers", Timed.checked == [], Timed.checked)

# the template is saved again mid-run with a new selection: checked afresh
_t.sleep(0.01)
with open(template, "w") as f:
    f.write("v2 — saved again")
Timed.checked = []
again = P5._clear_layer_selections(arcpy, project(selected=("Parcels",)), template=template)
check("changed template is checked again in full", len(set(Timed.checked)) == 6, Timed.checked)
check("and its new selection is cleared", again == ["Parcels"], again)

# a layer that cannot report stays on the list to check
P5._TEMPLATE_SELECTIONS.clear()
def unsure_project():
    m = project(selected=())
    m._layers[3]._raises = True
    return m
P5._clear_layer_selections(arcpy, unsure_project(), template=template)
Timed.checked = []
P5._clear_layer_selections(arcpy, unsure_project(), template=template)
check("a layer that cannot report is re-checked", set(Timed.checked) == {"Laterals"}, Timed.checked)

# no template given: every layer, every time, as before
Timed.checked = []
P5._clear_layer_selections(arcpy, project(selected=()))
P5._clear_layer_selections(arcpy, project(selected=()))
check("no template: every layer every time", len(Timed.checked) == 12, len(Timed.checked))
shutil.rmtree(tmpdir, ignore_errors=True)

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

"""Keeping one map project open between maps draws exactly what a fresh copy per map draws.

A fake ArcGIS project runs the real export code (project reset, selection
clearing, camera, layer filtering, layer report, titles, callout, arrow) for a
run of maps, the way Phase 4 drives it: append a map's features, export, delete
them.  The run is exported twice -- a fresh copy of the template per map, and
one copy kept open -- and what each PDF would show is compared: every layout
element, every layer's query and selection, and which features each layer draws.
"""
import copy, json, os, random, shutil, sys, tempfile, types
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
sys.modules.setdefault("pyodbc", types.ModuleType("pyodbc"))   # only ArcGIS Pro has it
import stub_arcpy as S
import backend.phase5.run as P5

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: " + str(detail)[:600]))
    if not cond: fails.append(name)

CONFIG = {
    "p4_ml_layer_path": "PMs_SSGRAVITYMAIN", "p4_ml_dest_path": "Trace_SSGRAVITYMAIN",
    "p4_ll_dest_path": "Lower_Laterals", "p4_parcels_dest_path": "Parcels_trace",
    "p4_ml_source_path": "ml_source", "p4_ll_source_path": "ll_source",
    "p4_parcels_source_path": "parcels_source",
}
SOURCE_OF = {"Trace_SSGRAVITYMAIN": "ml_source", "Lower_Laterals": "ll_source",
             "Parcels_trace": "parcels_source"}

# ---- the data, shared by Phase 4 and the export process -------------------
class World:
    def __init__(self):
        self.data = {p: {} for p in list(CONFIG.values())}
        self.next_oid = {p: 100 for p in self.data}
        self.added = {}            # (path, oid) -> clock when added
        self.clock = 0
        self.stale = False         # open layers miss rows added after the project opened
        self.opens = 0
        self.fail = {}             # what to break, and when

    def add(self, path, row):
        oid = self.next_oid[path]; self.next_oid[path] += 1
        self.clock += 1
        self.data[path][oid] = row
        self.added[(path, oid)] = self.clock
        return oid

W = None   # the current world

def matches(where, oid, row):
    return S._matches(where, oid, row)

class Cursor:
    def __init__(self, rows): self.rows = rows
    def __iter__(self): return iter(self.rows)
    def __enter__(self): return iter(self.rows)
    def __exit__(self, *a): return False

def project_rows(path, fields, where, visible=None):
    out = []
    for oid in sorted(W.data[path]):
        row = W.data[path][oid]
        if visible is not None and not visible(oid):
            continue
        if not matches(where, oid, row):
            continue
        out.append(tuple(oid if f in ("OID@", "OBJECTID") else row["SHAPE"] if f == "SHAPE@"
                         else row.get(f) for f in fields))
    return out

# ---- the fake arcpy.mp -----------------------------------------------------
class Layer:
    def __init__(self, project, spec):
        self.project = project
        self.name, self.dataSource = spec["name"], spec["source"]
        self.definitionQuery = spec.get("query", "")
        self._selection = set(spec.get("selection", []))
        self.isGroupLayer, self.isFeatureLayer, self.isBroken = False, True, False
    def supports(self, what): return True
    def getSelectionSet(self): return set(self._selection)
    def setSelectionSet(self, oids=None, method="NEW"): self._selection = set(oids or [])
    def sees(self, oid):
        if not W.stale:
            return True
        return W.added.get((self.dataSource, oid), 0) <= self.project.opened_at
    def drawn(self):
        return [r[0] for r in project_rows(self.dataSource, ["OID@"], self.definitionQuery, self.sees)]
    def state(self):
        return (self.name, self.definitionQuery, sorted(self._selection), self.drawn())

class Map:
    def __init__(self, project, spec):
        self.name = spec["name"]
        self.layers = [Layer(project, s) for s in spec["layers"]]
        self.referenceScale = spec["referenceScale"]
        self.spatialReference = types.SimpleNamespace(name="StatePlane II", factoryCode=2226)
    def listLayers(self): return list(self.layers)

class Camera:
    def __init__(self, el): self.el = el
    def setExtent(self, ext): self.el.d["camera"] = [ext.XMin, ext.YMin, ext.XMax, ext.YMax]
    def getExtent(self):
        c = self.el.d["camera"]
        return S.Ext(*c) if c else None

class Element:
    """Reads and writes its layout's CURRENT element record, like the real
    objects -- and goes stale (keeps the old record) after setDefinition."""
    def __init__(self, layout, record): self.layout, self.d = layout, record
    name = property(lambda self: self.d["name"])
    def _get(k): return property(lambda self: self.d[k], lambda self, v: self.d.__setitem__(k, v))
    elementPositionX, elementPositionY = _get("x"), _get("y")
    elementHeight, visible = _get("h"), _get("visible")
    @property
    def elementWidth(self): return self.d["w"]
    @elementWidth.setter
    def elementWidth(self, v): self.d["w"] = v
    @property
    def text(self):
        if "text" not in self.d:
            raise AttributeError("text")            # map frames and graphics have none
        return self.d["text"]
    @text.setter
    def text(self, v):
        self.d["text"] = v
        if self.d.get("point"):     # point text resizes around its anchor
            self.d["w"] = 0.1 * max(len(l) for l in v.split("\n"))
    @property
    def camera(self): return Camera(self)
    @property
    def map(self): return self.layout.project.map
    @map.setter
    def map(self, m): self.d["map"] = m.name
    def getDefinition(self, version):
        if self.d["type"] == "GRAPHIC_ELEMENT":
            if W.fail.get("arrow") == W.current:
                raise RuntimeError("arrow CIM unreadable")
            ns = types.SimpleNamespace
            return ns(graphic=ns(line=ns(paths=copy.deepcopy(self.d["paths"]))))
        ns = types.SimpleNamespace
        return ns(anchor=self.d["anchor"], graphic=ns(symbol=ns(symbol=ns(horizontalAlignment=self.d["align"]))))
    def setDefinition(self, cim): self.d["paths"] = copy.deepcopy(cim.graphic.line.paths)

class Layout:
    def __init__(self, project, spec):
        self.project, self.name = project, spec["name"]
        self.state = copy.deepcopy(spec)
    def listElements(self, kind=None, name=None):
        return [Element(self, r) for r in self.state["elements"]
                if (kind is None or r["type"] == kind) and (name is None or r["name"] == name)]
    def getDefinition(self, version): return copy.deepcopy(self.state)
    def setDefinition(self, definition):
        if W.fail.get("reset") == W.current:
            raise RuntimeError("layout definition rejected")
        if W.fail.get("silent_reset") == W.current:
            return                                  # accepted, but nothing put back
        self.state = copy.deepcopy(definition)
    def exportToPDF(self, path):
        W.exports[W.current] = snapshot(self.project, self)

class Project:
    def __init__(self, path):
        with open(path) as f:
            spec = json.load(f)
        W.opens += 1
        self.opened_at = W.clock
        self.filePath = path
        self.map = Map(self, spec["map"])
        self.layouts = [Layout(self, s) for s in spec["layouts"]]
    def listMaps(self, name=None): return [self.map] if name in (None, self.map.name) else []
    def listLayouts(self, name=None): return [l for l in self.layouts if name in (None, l.name)]
    def saveACopy(self, path): W.drafts[W.current] = snapshot(self, None)

def snapshot(project, layout):
    return {
        "layout": layout.name if layout else None,
        "layouts": {l.name: copy.deepcopy(l.state) for l in project.layouts},
        "layers": [l.state() for l in project.map.layers],
        "referenceScale": project.map.referenceScale,
    }

def make_arcpy():
    a = types.SimpleNamespace()
    a.mp = types.SimpleNamespace(ArcGISProject=Project)
    a.Extent = S.Ext
    a.Point, a.Array, a.Polygon = S.Point, S.Array, S.Polygon
    a.Describe = lambda p: types.SimpleNamespace(spatialReference=S.SR())
    def search(target, fields, where=None):
        if isinstance(target, Layer):
            both = " AND ".join(q for q in (target.definitionQuery, where) if q)
            return Cursor(project_rows(target.dataSource, fields, both, target.sees))
        if W.fail.get("filter") == W.current and target == "Parcels_trace":
            raise RuntimeError("cannot read Parcels_trace")      # sends it to the selection tools
        return Cursor(project_rows(target, fields, where))
    a.da = types.SimpleNamespace(SearchCursor=search)
    def select_by_location(layer, how, polygon):
        layer.setSelectionSet(layer.drawn())
    def clear_selection(layer, how):
        if W.fail.get("filter") == W.current:
            raise RuntimeError("Cannot acquire a lock")          # the selection is left behind
        layer.setSelectionSet([])
    a.management = types.SimpleNamespace(
        SelectLayerByAttribute=clear_selection, SelectLayerByLocation=select_by_location)
    return a

# ---- the template --------------------------------------------------------
def text(name, txt, x, y, anchor="BottomLeftCorner", align="Center", point=True, w=None, h=0.3, visible=True):
    return {"type": "TEXT_ELEMENT", "name": name, "text": txt, "x": x, "y": y,
            "w": w if w is not None else 0.1 * max(len(l) for l in txt.split("\n")),
            "h": h, "visible": visible, "anchor": anchor, "align": align, "point": point}

def layout(name, fw, fh):
    return {"name": name, "elements": [
        {"type": "MAPFRAME_ELEMENT", "name": "Map Frame", "x": 0.5, "y": 0.5, "w": fw, "h": fh,
         "camera": [0, 0, 100, 100], "map": "High Freq ML Data Map", "visible": True},
        text("Figure", "Figure XXX - Area of Interest\nStop The Clog Strategy", 4.0, fh + 0.7),
        text("date", "January 1, 2026", fw - 1.5, fh + 0.7, anchor="BottomRightCorner", align="Right"),
        text("Auto HFML Callout", "callout", 1.0, 1.0, point=False, w=1.5, h=1.0, visible=False),
        {"type": "GRAPHIC_ELEMENT", "name": "Auto HFML Arrow", "x": 0, "y": 0, "w": 1, "h": 1,
         "visible": False, "paths": [[[0, 0], [1, 1]]]},
        text("Disclosure", "For Internal Use Only", 0.5, 0.2, align="Left"),
    ]}

TEMPLATE = {
    "map": {"name": "High Freq ML Data Map", "referenceScale": 2400, "layers": [
        {"name": "Spill", "source": "ml_source", "selection": [1]},     # saved selection
        {"name": "High Freq ML PMs", "source": "PMs_SSGRAVITYMAIN"},
        {"name": "Trace_SSGRAVITYMAIN", "source": "Trace_SSGRAVITYMAIN"},
        {"name": "Trace Upstream High Freq PM", "source": "Trace_SSGRAVITYMAIN", "query": "STATUS = 'OPERATING'"},
        {"name": "Lower_Laterals", "source": "Lower_Laterals",
         "query": "STATUS IN ('OPERATING', 'TOBEMOD')", "selection": [5]},
        {"name": "Parcels in the Trace", "source": "Parcels_trace"},
    ]},
    "layouts": [layout("Horizontal Layout", 16.0, 7.5), layout("Vertical Layout", 10.0, 13.5)],
}

# ---- Phase 4's side: append, export, delete -------------------------------
def line(x, y, dx, dy): return S.Geom("line", [S.Pt(x, y), S.Pt(x + dx, y + dy)])

def plan_run(seed, n):
    """The maps of one run.  Some sit right next to an earlier one."""
    rnd = random.Random(seed)
    maps, spots = [], []
    for i in range(n):
        if spots and rnd.random() < 0.5:
            x, y = rnd.choice(spots); x += rnd.uniform(-300, 300); y += rnd.uniform(-300, 300)
        else:
            x, y = rnd.uniform(0, 50_000), rnd.uniform(0, 50_000)
        spots.append((x, y))
        tall = rnd.random() < 0.4
        maps.append({
            "pmnum": str(1000 + i), "x": x, "y": y, "tall": tall,
            "gridno": "-".join(str(rnd.randint(100, 999)) for _ in range(rnd.randint(1, 3))),
            "mls": rnd.randint(0, 6), "lls": rnd.randint(0, 12), "parcels": rnd.randint(0, 12),
        })
    return maps

def run(maps, keep_open, tmp, fail=None, change_template_before=None, stale=False):
    global W
    os.makedirs(tmp)
    W = World(); W.exports, W.drafts = {}, {}; W.fail = dict(fail or {}); W.stale = stale
    template = os.path.join(tmp, "TEST.aprx")
    with open(template, "w") as f:
        json.dump(TEMPLATE, f)
    P5._TEMPLATE_SELECTIONS.clear()
    P5.close_open_project()
    P5._OPEN_PROJECT["allowed"] = keep_open
    real = (P5._read_pm_row, P5._get_phase4_feature_info, P5._get_callout_text)
    lines, failed = [], []
    current = {}
    P5._read_pm_row = lambda *a, **k: {"gridno": current["gridno"]}
    def feature_info(arcpy, hfml, config, log_fn=None, pm_row=None):
        exts = [W.data[p][o]["SHAPE"].extent for p, key in
                (("PMs_SSGRAVITYMAIN", "seed_oids"), ("ml_source", "ml_oids"),
                 ("ll_source", "ll_oids"), ("parcels_source", "parcel_oids")) for o in hfml[key]]
        union = S.Ext(min(e.XMin for e in exts), min(e.YMin for e in exts),
                      max(e.XMax for e in exts), max(e.YMax for e in exts))
        seed = W.data["PMs_SSGRAVITYMAIN"][hfml["seed_oids"][0]]["SHAPE"].extent
        return union, S.Pt((seed.XMin + seed.XMax) / 2, (seed.YMin + seed.YMax) / 2), seed
    P5._get_phase4_feature_info = feature_info
    def callout_text(*a, **k):
        if W.fail.get("callout") == W.current:
            raise RuntimeError("callout text failed")
        return f"ML {current['gridno']}\n6\" VCP\n\nNotes:\nObservations: None"
    P5._get_callout_text = callout_text
    arcpy = make_arcpy()
    try:
        for m in maps:
            if change_template_before == m["pmnum"]:
                changed = copy.deepcopy(TEMPLATE)
                changed["layouts"][0]["elements"][1]["text"] = "Figure XXX - Area of Interest (rev)\nStop The Clog Strategy"
                with open(template, "w") as f:
                    json.dump(changed, f)
                st = os.stat(template); os.utime(template, (st.st_atime, st.st_mtime + 10))
            current.update(m); W.current = m["pmnum"]
            x, y = m["x"], m["y"]
            w, h = (60, 400) if m["tall"] else (400, 60)
            seed_geom = line(x, y, w * 0.3, h * 0.3)
            job = {"pmnum": m["pmnum"], "aprx_path": template,
                   "pdf_path": os.path.join(tmp, "out", m["pmnum"] + ".pdf"),
                   "draft_aprx_path": os.path.join(tmp, "out", "Temp GIS Maps", m["pmnum"] + ".aprx"),
                   "seed_oids": [W.add("PMs_SSGRAVITYMAIN", {"SHAPE": seed_geom, "PM_Mainlines_pmnum": m["pmnum"]})],
                   "ml_oids": [], "ll_oids": [], "parcel_oids": []}
            appended = []
            for dest, key, count in (("Trace_SSGRAVITYMAIN", "ml_oids", m["mls"]),
                                     ("Lower_Laterals", "ll_oids", m["lls"]),
                                     ("Parcels_trace", "parcel_oids", m["parcels"])):
                for k in range(count):
                    g = line(x + (k % 4) * w / 4, y + (k // 4) * h / 4, w / 5, h / 5)
                    row = {"SHAPE": g, "STATUS": "OPERATING"}
                    oid = W.add(dest, row)
                    W.data[SOURCE_OF[dest]][oid] = row
                    job[key].append(oid); appended.append((dest, oid))
            appended.append(("PMs_SSGRAVITYMAIN", job["seed_oids"][0]))
            try:
                P5.export_hfml_map(arcpy, job, CONFIG, "November 19, 2026", {}, lines.append)
            except Exception as exc:
                failed.append(m["pmnum"])
                W.exports.pop(m["pmnum"], None); W.drafts.pop(m["pmnum"], None)
            for dest, oid in appended:                      # Phase 4 cleans up
                W.data[dest].pop(oid, None)
        P5.close_open_project()                             # the export process exits
    finally:
        P5._read_pm_row, P5._get_phase4_feature_info, P5._get_callout_text = real
        P5._OPEN_PROJECT["allowed"] = False
    leftovers = [n for n in os.listdir(tmp) if n.endswith("_tmp.aprx")]
    with open(template) as f:
        template_after = f.read()
    return {"exports": W.exports, "drafts": W.drafts, "opens": W.opens, "lines": lines,
            "failed": failed, "leftovers": leftovers, "template": template_after}

def diff(a, b):
    for key in sorted(set(a) | set(b)):
        if a.get(key) != b.get(key):
            return key
    return None

tmp = tempfile.mkdtemp()
try:
    # 1. Many runs: kept open == fresh copy per map, map for map.
    for seed in range(25):
        maps = plan_run(seed, 8)
        fresh = run(maps, False, os.path.join(tmp, f"f{seed}"))
        kept = run(maps, True, os.path.join(tmp, f"k{seed}"))
        check(f"run {seed}: every PDF identical", kept["exports"] == fresh["exports"],
              diff(kept["exports"], fresh["exports"]))
        check(f"run {seed}: every saved map project identical", kept["drafts"] == fresh["drafts"],
              diff(kept["drafts"], fresh["drafts"]))
        check(f"run {seed}: project opened once instead of per map",
              kept["opens"] == 1 and fresh["opens"] == len(maps), (kept["opens"], fresh["opens"]))
        check(f"run {seed}: template never written", kept["template"] == json.dumps(TEMPLATE))
        check(f"run {seed}: no working copies left", kept["leftovers"] == [] and fresh["leftovers"] == [],
              (kept["leftovers"], fresh["leftovers"]))

    # What a PDF shows, spelled out once: only this map's features, nothing selected.
    maps = plan_run(3, 8)
    kept = run(maps, True, os.path.join(tmp, "spell"))
    last = kept["exports"][maps[-1]["pmnum"]]
    layers = {name: (query, sel, drawn) for name, query, sel, drawn in last["layers"]}
    check("HFML layer draws only this HFML", len(layers["High Freq ML PMs"][2]) == 1, layers["High Freq ML PMs"])
    check("no layer has a selection", all(not sel for _q, sel, _d in layers.values()), layers)
    check("callout shows this map's ML", any(e["name"] == "Auto HFML Callout" and maps[-1]["gridno"] in e["text"]
          for e in last["layouts"][last["layout"]]["elements"]))

    # 2. The open copy cannot see rows added after it opened: every map after
    #    the first is caught by the count check and redone on a fresh copy.
    maps = plan_run(7, 6)
    for m in maps:
        m["lls"] = max(m["lls"], 2)
    fresh = run(maps, False, os.path.join(tmp, "sf"))
    kept = run(maps, True, os.path.join(tmp, "sk"), stale=True)
    check("stale open copy: PDFs still identical", kept["exports"] == fresh["exports"],
          diff(kept["exports"], fresh["exports"]))
    check("stale open copy: each later map redone on a fresh copy",
          sum("redoing this map on a fresh copy" in l for l in kept["lines"]) == len(maps) - 1,
          [l for l in kept["lines"] if "redoing" in l])
    check("stale open copy: the redo says which layer was short",
          any("Lower_Laterals" in l and "where Phase 4 appended" in l for l in kept["lines"]))

    # 3. The template saved mid-run: a fresh copy is opened from the new file.
    maps = plan_run(11, 6)
    fresh = run(maps, False, os.path.join(tmp, "tf"), change_template_before=maps[3]["pmnum"])
    kept = run(maps, True, os.path.join(tmp, "tk"), change_template_before=maps[3]["pmnum"])
    check("template changed mid-run: PDFs identical", kept["exports"] == fresh["exports"],
          diff(kept["exports"], fresh["exports"]))
    check("template changed mid-run: reopened once", kept["opens"] == 2, kept["opens"])
    check("  and says so", any("map project file has changed" in l for l in kept["lines"]))

    # 4. A reset that fails opens a fresh copy; a map that fails leaves nothing
    #    behind for the next one; an arrow that fails is back on the next map.
    maps = plan_run(13, 8)
    # each followed by a map that reuses the open copy
    fail = {"filter": maps[1]["pmnum"], "reset": maps[3]["pmnum"], "callout": maps[4]["pmnum"],
            "arrow": maps[6]["pmnum"]}
    fresh = run(maps, False, os.path.join(tmp, "xf"), fail=fail)
    kept = run(maps, True, os.path.join(tmp, "xk"), fail=fail)
    check("failures: PDFs identical", kept["exports"] == fresh["exports"], diff(kept["exports"], fresh["exports"]))
    check("failures: the same map failed both ways", kept["failed"] == fresh["failed"] == [maps[4]["pmnum"]],
          (kept["failed"], fresh["failed"]))
    check("failures: reset failure reported", any("could not reset the open map project" in l for l in kept["lines"]))
    arrow_map = kept["exports"][maps[6]["pmnum"]]
    next_map = kept["exports"][maps[7]["pmnum"]]
    def arrow(snap): return next(e for e in snap["layouts"][snap["layout"]]["elements"] if e["name"] == "Auto HFML Arrow")
    check("failures: arrow hidden on its map, shown again on the next",
          not arrow(arrow_map)["visible"] and arrow(next_map)["visible"])
    check("failures: no working copies left", kept["leftovers"] == [], kept["leftovers"])
    def selection(snap, name): return next(sel for n, _q, sel, _d in snap["layers"] if n == name)
    stuck = kept["exports"][maps[1]["pmnum"]]
    check("failures: a selection left by the selection tools...", selection(stuck, "Parcels in the Trace"),
          stuck["layers"])
    check("  ...is gone by the next map that exports", not any(
          selection(kept["exports"][m["pmnum"]], "Parcels in the Trace")
          for m in maps[2:] if m["pmnum"] in kept["exports"]))

    # 5. A reset that reports no error but leaves the layout as the last map had
    #    it is caught by comparing the elements with the template's.
    maps = plan_run(19, 5)
    fresh = run(maps, False, os.path.join(tmp, "qf"))
    kept = run(maps, True, os.path.join(tmp, "qk"), fail={"silent_reset": maps[2]["pmnum"]})
    check("silent reset failure: PDFs identical", kept["exports"] == fresh["exports"],
          diff(kept["exports"], fresh["exports"]))
    check("silent reset failure: caught and a fresh copy opened",
          kept["opens"] == 2 and any("did not return to the template" in l for l in kept["lines"]),
          (kept["opens"], [l for l in kept["lines"] if "reset" in l]))

    # 6. Switched off: a fresh copy per map, as before.
    maps = plan_run(17, 4)
    CONFIG["p4_export_keep_project_open"] = False
    off = run(maps, True, os.path.join(tmp, "off"))
    CONFIG.pop("p4_export_keep_project_open")
    check("p4_export_keep_project_open false: a fresh copy per map", off["opens"] == len(maps), off["opens"])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("FAILURES: " + (", ".join(fails[:10]) + (" ..." if len(fails) > 10 else "") if fails else "none"))
sys.exit(1 if fails else 0)

"""A small arcpy stand-in with real 2D geometry and arcpy's selection rules.

Only what the Phase 4 selection, append and map-scoping code touches.  Mirrors
the behaviours that matter for correctness:

  * a tool given a layer as input uses the layer's selection, and when that
    selection is empty it uses every feature (arcpy's documented rule);
  * a cursor on a layer honours the selection even when it is empty -- after
    a select that matched nothing, the cursor returns nothing.  Observed in a
    real run on 2026-11-17 (HFML 32674); an earlier version of this stand-in
    had cursors read every feature too, which was wrong;
  * a layer made from a layer with a selection contains only the selection;
  * tools count their calls, so a test can see which kinds were made.

It is a model of arcpy, not arcpy: the tests prove the new code matches the
old under these rules, and the real runs are what confirm the rules.
"""
import math, re, types

class Pt:
    def __init__(self, x, y): self.X, self.Y = float(x), float(y)

class Ext:
    def __init__(self, x1, y1, x2, y2): self.XMin, self.YMin, self.XMax, self.YMax = x1, y1, x2, y2

def _pt_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

def _orient(ax, ay, bx, by, cx, cy):
    v = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)

def _on_seg(ax, ay, bx, by, cx, cy):
    return min(ax, bx) - 1e-12 <= cx <= max(ax, bx) + 1e-12 and min(ay, by) - 1e-12 <= cy <= max(ay, by) + 1e-12

def _segs_cross(a, b, c, d):
    o1, o2 = _orient(*a, *b, *c), _orient(*a, *b, *d)
    o3, o4 = _orient(*c, *d, *a), _orient(*c, *d, *b)
    if o1 != o2 and o3 != o4:
        return True
    return ((o1 == 0 and _on_seg(*a, *b, *c)) or (o2 == 0 and _on_seg(*a, *b, *d))
            or (o3 == 0 and _on_seg(*c, *d, *a)) or (o4 == 0 and _on_seg(*c, *d, *b)))

def _seg_seg(a, b, c, d):
    if _segs_cross(a, b, c, d):
        return 0.0
    return min(_pt_seg(*a, *c, *d), _pt_seg(*b, *c, *d), _pt_seg(*c, *a, *b), _pt_seg(*d, *a, *b))

class Geom:
    """kind: point | line | rect.  line holds vertex list; rect holds bounds."""
    def __init__(self, kind, data, sr="2226"):
        self.kind, self.data, self.spatialReference = kind, data, sr
        if kind == "point":
            p = data
            self.firstPoint = self.lastPoint = p
            self.extent = Ext(p.X, p.Y, p.X, p.Y)
        elif kind == "line":
            self.firstPoint, self.lastPoint = data[0], data[-1]
            xs = [p.X for p in data]; ys = [p.Y for p in data]
            self.extent = Ext(min(xs), min(ys), max(xs), max(ys))
        else:
            x1, y1, x2, y2 = data
            self.firstPoint = self.lastPoint = Pt(x1, y1)
            self.extent = Ext(x1, y1, x2, y2)

    def _segs(self):
        if self.kind == "line":
            return [((a.X, a.Y), (b.X, b.Y)) for a, b in zip(self.data, self.data[1:])]
        if self.kind == "rect":
            x1, y1, x2, y2 = self.data
            c = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
            return [(c[i], c[(i + 1) % 4]) for i in range(4)]
        p = self.data
        return [((p.X, p.Y), (p.X, p.Y))]

    def _contains(self, x, y):
        if self.kind != "rect":
            return False
        x1, y1, x2, y2 = self.data
        return x1 <= x <= x2 and y1 <= y <= y2

    def distanceTo(self, other):
        for a, b in ((self, other), (other, self)):
            if a.kind == "rect":
                for (sx, sy), _ in b._segs():
                    if a._contains(sx, sy):
                        return 0.0
                for _, (ex, ey) in b._segs():
                    if a._contains(ex, ey):
                        return 0.0
        return min(_seg_seg(a1, a2, b1, b2) for a1, a2 in self._segs() for b1, b2 in other._segs())

    def disjoint(self, other):
        return self.distanceTo(other) > 0

def Point(x, y): return Pt(x, y)
def PointGeometry(pt, sr=None): return Geom("point", pt, sr)
def Array(points): return list(points)
def Polygon(points, sr=None):
    xs = [p.X for p in points]; ys = [p.Y for p in points]
    return Geom("rect", (min(xs), min(ys), max(xs), max(ys)), sr)

class Field:
    def __init__(self, name, type_="String", editable=True):
        self.name, self.type, self.editable = name, type_, editable

class SR:
    def __init__(self, code=2226, name="NAD_1983_StatePlane_California_II_FIPS_0402_Feet"):
        self.factoryCode, self.name = code, name

# ---- where clauses ----------------------------------------------------------
def _split_and(expr):
    parts, depth, quote, i, start = [], 0, False, 0, 0
    up = expr.upper()
    while i < len(expr):
        c = expr[i]
        if c == "'":
            quote = not quote
        elif not quote and c == "(":
            depth += 1
        elif not quote and c == ")":
            depth -= 1
        elif not quote and depth == 0 and up.startswith(" AND ", i):
            parts.append(expr[start:i]); i += 5; start = i; continue
        i += 1
    parts.append(expr[start:])
    return [p.strip() for p in parts]

def _strip_parens(expr):
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        for i, c in enumerate(expr):
            depth += c == "("; depth -= c == ")"
            if depth == 0 and i < len(expr) - 1:
                return expr
        expr = expr[1:-1].strip()
    return expr

def _get(row, field):
    for k, v in row.items():
        if k.lower() == field.lower():
            return v
    return None

def _matches(where, oid, row):
    if not where:
        return True
    expr = _strip_parens(where)
    parts = _split_and(expr)
    if len(parts) > 1:
        return all(_matches(p, oid, row) for p in parts)
    atom = _strip_parens(parts[0])
    if atom.replace(" ", "") == "1=0":
        return False
    m = re.match(r"^(\w+)\s+IN\s*\((.*)\)$", atom, re.I | re.S)
    if m:
        field, values = m.group(1), m.group(2)
        if field.upper() == "OBJECTID":
            return oid in {int(v) for v in values.split(",") if v.strip()}
        wanted = {v.strip().strip("'") for v in values.split(",")}
        return _get(row, field) in wanted
    m = re.match(r"^(\w+)\s*=\s*'(.*)'$", atom, re.I)
    if m:
        return _get(row, m.group(1)) == m.group(2)
    raise ValueError(f"stub cannot parse where clause: {atom!r}")

_DIST = re.compile(r"([\d.]+)\s*(Foot|Feet)", re.I)
_IN = re.compile(r"OBJECTID\s+IN\s*\(([^)]*)\)", re.I)

class Arcpy:
    def __init__(self, datasets, schemas=None, srs=None):
        # datasets: {path: {oid: {"SHAPE": Geom, ...}}}
        self.datasets = {p: {o: dict(r) for o, r in rows.items()} for p, rows in datasets.items()}
        self.schemas = dict(schemas or {})
        self.srs = dict(srs or {})
        self.Polygon = Polygon
        self.Array = Array
        self.next_oid = {p: (max(rows) if rows else 0) + 1 for p, rows in self.datasets.items()}
        self.layers = {}
        self.calls = {}
        self.da = types.SimpleNamespace(SearchCursor=self._search, InsertCursor=self._insert,
                                        UpdateCursor=self._update)
        self.Point = Point
        self.PointGeometry = PointGeometry
        m = self.management = types.SimpleNamespace()
        for name in ("MakeFeatureLayer", "SelectLayerByLocation", "SelectLayerByAttribute",
                     "Delete", "CreateFeatureclass", "AddField", "GetCount", "ClearWorkspaceCache",
                     "Append"):
            setattr(m, name, self._counted(name, getattr(self, "_" + name)))

    def _counted(self, name, fn):
        def wrapper(*a, **k):
            self.calls[name] = self.calls.get(name, 0) + 1
            return fn(*a, **k)
        return wrapper

    # ---- feature sets ------------------------------------------------------
    def _dataset_of(self, target):
        return self.layers[target]["dataset"] if target in self.layers else target

    def _effective(self, target, as_tool_input=False):
        """Features a reader sees.  selection None = nothing selected;
        an empty set = a selection that matched nothing."""
        if target in self.layers:
            lyr = self.layers[target]
            sel = lyr["selection"]
            if sel is None or (as_tool_input and not sel):
                return set(lyr["base"])
            return set(sel)
        return set(self.datasets[target])

    def _where(self, oids, where, dataset=None):
        if not where:
            return set(oids)
        rows = self.datasets.get(dataset, {}) if dataset else {}
        return {o for o in oids if _matches(where, o, rows.get(o, {}))}

    def ListFields(self, target):
        ds = self._dataset_of(target)
        if ds in self.schemas:
            return list(self.schemas[ds])
        names = []
        for row in self.datasets[ds].values():
            names += [k for k in row if k != "SHAPE" and k not in names]
        return [Field("OBJECTID", "OID", False), Field("Shape", "Geometry")] + [Field(n) for n in names]

    # ---- arcpy API ---------------------------------------------------------
    def Exists(self, name): return name in self.layers or name in self.datasets

    def Describe(self, target):
        ds = self._dataset_of(target)
        rows = self.datasets[ds]
        sr = SR(*self.srs[ds]) if ds in self.srs else SR()
        fid = ""
        if target in self.layers and self.layers[target]["selection"]:  # non-empty only
            fid = "; ".join(str(o) for o in sorted(self.layers[target]["selection"]))
        return types.SimpleNamespace(spatialReference=sr, FIDSet=fid)

    def _MakeFeatureLayer(self, source, name, where=None):
        dataset = self._dataset_of(source)
        base = self._where(self._effective(source, as_tool_input=True), where, dataset)
        self.layers[name] = {"dataset": dataset, "base": base, "selection": None}

    def _Delete(self, name):
        self.layers.pop(name, None)
        self.datasets.pop(name, None)

    def _CreateFeatureclass(self, ws, name, kind, spatial_reference=None):
        path = f"{ws}\\{name}"
        self.datasets[path] = {}
        self.next_oid[path] = 1

    def _AddField(self, *a, **k): pass
    def _ClearWorkspaceCache(self, *a, **k): pass

    def _GetCount(self, target): return [str(len(self._effective(target)))]

    def _apply(self, layer, chosen, kind):
        lyr = self.layers[layer]
        if kind == "NEW_SELECTION":
            lyr["selection"] = set(chosen)
        elif kind == "ADD_TO_SELECTION":
            lyr["selection"] = set(lyr["selection"] or ()) | set(chosen)
        else:
            raise ValueError(kind)

    def _SelectLayerByAttribute(self, layer, kind="NEW_SELECTION", where=None):
        if kind == "CLEAR_SELECTION":
            self.layers[layer]["selection"] = None
            return
        self._apply(layer, self._where(self.layers[layer]["base"], where, self.layers[layer]["dataset"]), kind)

    def _SelectLayerByLocation(self, layer, relation, select_features,
                               search_distance=None, selection_type="NEW_SELECTION"):
        distance = float(_DIST.search(search_distance).group(1)) if search_distance else 0.0
        src = self.datasets[self._dataset_of(select_features)]
        sel_geoms = [src[o]["SHAPE"] for o in self._effective(select_features, as_tool_input=True)]
        rows = self.datasets[self.layers[layer]["dataset"]]
        # XY tolerance, as the real select applies it
        limit = distance + 0.001
        chosen = {o for o in self.layers[layer]["base"]
                  if any(rows[o]["SHAPE"].distanceTo(g) <= limit for g in sel_geoms)}
        self._apply(layer, chosen, selection_type)

    def _Append(self, inputs, target, schema_type="NO_TEST"):
        src_ds = self._dataset_of(inputs)
        src_rows = self.datasets[src_ds]
        src_names = {f.name.lower(): f.name for f in self.ListFields(src_ds)
                     if f.type not in ("OID", "Geometry", "GlobalID")}
        tgt_fields = [f for f in self.ListFields(target)
                      if f.type not in ("OID", "Geometry", "GlobalID") and f.editable]
        for oid in sorted(self._effective(inputs, as_tool_input=True)):
            r = src_rows[oid]
            row = {"SHAPE": r["SHAPE"]}
            for f in tgt_fields:
                src_name = src_names.get(f.name.lower())
                row[f.name] = _get(r, src_name) if src_name else None
            new = self.next_oid[target]; self.next_oid[target] = new + 1
            self.datasets[target][new] = row

    def _search(self, target, fields, where=None):
        rows = self.datasets[self._dataset_of(target)]
        oids = self._where(self._effective(target), where, self._dataset_of(target))
        out = []
        for oid in sorted(oids):
            r = rows[oid]
            vals = []
            for f in fields:
                if f in ("OID@", "OBJECTID"):
                    vals.append(oid)
                elif f == "SHAPE@":
                    vals.append(r["SHAPE"])
                elif f == "SHAPE@XY":
                    p = r["SHAPE"].firstPoint
                    vals.append((p.X, p.Y))
                else:
                    vals.append(_get(r, f))
            out.append(tuple(vals))
        return _Cursor(out)

    def _insert(self, path, fields):
        return _Inserter(self, path, fields)

    def _update(self, path, fields, where=None):
        return _Updater(self, path, fields, where)

class _Cursor:
    def __init__(self, rows): self.rows = rows
    def __iter__(self): return iter(self.rows)
    def __enter__(self): return iter(self.rows)
    def __exit__(self, *a): return False

class _Inserter:
    def __init__(self, arcpy, path, fields): self.a, self.path, self.fields = arcpy, path, fields
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def insertRow(self, values):
        row = {}
        for f, v in zip(self.fields, values):
            row["SHAPE" if f == "SHAPE@" else f] = v
        oid = self.a.next_oid[self.path]
        self.a.next_oid[self.path] = oid + 1
        self.a.datasets[self.path][oid] = row
        return oid

class _Updater:
    def __init__(self, arcpy, path, fields, where):
        self.a, self.path, self.fields = arcpy, path, fields
        self.oids = sorted(arcpy._where(set(arcpy.datasets[path]), where, path))
        self.current = None
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self):
        for oid in self.oids:
            if oid in self.a.datasets[self.path]:
                self.current = oid
                row = self.a.datasets[self.path][oid]
                yield [oid if f in ("OID@", "OBJECTID") else _get(row, f) for f in self.fields]
    def deleteRow(self):
        self.a.datasets[self.path].pop(self.current, None)

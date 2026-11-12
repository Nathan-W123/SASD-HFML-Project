import math
import os
import sys
import time
from contextlib import contextmanager
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import json

from backend.phase4.network_cache import (
    Phase4Cache, dataset_signature, default_cache_dir, free_gb, signature_is_weak,
)
from config_loader import load_config

BUFFER_DISTANCE = "50000 Feet"
LL_SEARCH_DISTANCE = "1 Foot"
# Same distance in map units (feet), for excluding LLs that only tie in at the
# HFML's downstream manhole.
DOWNSTREAM_MANHOLE_TOLERANCE = 1.0
# Laterals often stop a little short of the property line, so an LL end that
# lies in no parcel takes the nearest parcel within this many feet.
PARCEL_SEARCH_FEET = 5.0
# Longest run of pipes digitized against the flow the trace will follow from a
# traced manhole; a longer run is treated as a real flow split (reported only).
MAX_AGAINST_FLOW_RUN = 10
ARCPY_RETRY_ATTEMPTS = 3
# Beyond this a WHERE clause risks hitting backend query-length limits, so
# the ML source is read in full and filtered in Python instead.
MAX_WHERE_CLAUSE_CHARS = 30000
# How far from the edge of a finished trace to look for an unconnected pipe
# end.  Anything found is reported, never followed: a short distance is what a
# gap in the data looks like, and the branch above it is missing from the map.
GAP_REPORT_RADIUS_FEET = 2.0
MAX_GAP_REPORT_LINES = 10
# Buckets for the once-per-cache-build report on how close manholes sit to each
# other without sharing a key.  The shape of this picks the snap tolerance.
GAP_HISTOGRAM_BUCKETS = (0.01, 0.1, 0.5, 1.0)
# Beta testing: only map this many new HFMLs per run.  Raise (or remove) once
# the full monthly batch should be processed.
MAX_HFMLS_PER_RUN = 5
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ADDED_MLS_PATH = os.path.join(PROJECT_ROOT, "temp", "added_mls.json")
PHASE4_MAP_OUTPUT_PATH = os.path.join(PROJECT_ROOT, "temp", "phase4_mapped_hfmls.json")


def _import_arcpy():
    try:
        import arcpy
        return arcpy
    except Exception as exc:
        raise RuntimeError(
            "ArcGIS Pro Python is not loading correctly. Run the pipeline from "
            "the SASD_HFML.pyt toolbox inside ArcGIS Pro. "
            f"Current interpreter: {sys.executable}. Original import error: {exc}"
        ) from exc


def _clear_workspace_cache(arcpy):
    try:
        arcpy.management.ClearWorkspaceCache()
    except Exception:
        pass


def _with_arcpy_retry(arcpy, action, context):
    last_exc = None
    for attempt in range(1, ARCPY_RETRY_ATTEMPTS + 1):
        try:
            return action()
        except Exception as exc:
            last_exc = exc
            if attempt == ARCPY_RETRY_ATTEMPTS:
                break
            _clear_workspace_cache(arcpy)
            time.sleep(attempt)
    raise RuntimeError(
        f"{context} failed after {ARCPY_RETRY_ATTEMPTS} attempts. Last error: {last_exc}"
    ) from last_exc


def _replace_temp_layer(arcpy, layer_name, source, where_clause=None):
    def _make_layer():
        if arcpy.Exists(layer_name):
            arcpy.management.Delete(layer_name)
        arcpy.management.MakeFeatureLayer(source, layer_name, where_clause)
        return layer_name

    return _with_arcpy_retry(arcpy, _make_layer, f"Failed to open feature layer from {source}")


def _get_field_names(arcpy, dataset):
    return _with_arcpy_retry(
        arcpy,
        lambda: [field.name for field in arcpy.ListFields(dataset)],
        f"Failed to list fields for {dataset}",
    )


def _norm_id(value):
    # PM_Mainlines, the HFML layer and the ML source don't necessarily store IDs
    # with the same field type, so 500123, 500123.0 and "500123 " must match.
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        return text
    return str(int(number)) if number.is_integer() else text


def _geom_to_temp_fc(arcpy, geom, name):
    path = f"in_memory\\{name}"
    if arcpy.Exists(path):
        arcpy.management.Delete(path)
    arcpy.management.CreateFeatureclass("in_memory", name, "POLYLINE", spatial_reference=geom.spatialReference)
    with arcpy.da.InsertCursor(path, ["SHAPE@"]) as cursor:
        cursor.insertRow([geom])
    return path




def _require_dataset(arcpy, path, label, log_fn):
    exists = False
    for attempt in range(1, ARCPY_RETRY_ATTEMPTS + 1):
        try:
            exists = bool(arcpy.Exists(path))
        except Exception:
            exists = False
        if exists:
            break
        if attempt < ARCPY_RETRY_ATTEMPTS:
            _clear_workspace_cache(arcpy)
            time.sleep(attempt)
    if not exists:
        raise RuntimeError(f"{label} does not exist: {path}")
    log_fn(f"{label}: {path}")


def _require_write_lock(arcpy, path, label):
    if not arcpy.TestSchemaLock(path):
        raise RuntimeError(
            f"{label} is locked and cannot be written: {path}. "
            "Close ArcGIS Pro maps, attribute tables, edit sessions, Catalog previews, "
            "or any other tools using this geodatabase, then rerun Phase 4."
        )


def _describe_sr(arcpy, path):
    """A short, comparable description of a dataset's coordinate system."""
    try:
        sr = arcpy.Describe(path).spatialReference
        return f"{sr.name} (WKID {sr.factoryCode})"
    except Exception as exc:
        return f"unreadable ({exc})"


def _report_spatial_references(arcpy, config, log_fn):
    """Log the coordinate system of each source/destination pair.

    Append reprojects between differing coordinate systems, which moves the
    geometry slightly.  When a feature is appended and then does not match the
    map's extent filter, this is the first thing to check.  Read-only.
    """
    pairs = (
        ("ML", "p4_ml_source_path", "p4_ml_dest_path"),
        ("LL", "p4_ll_source_path", "p4_ll_dest_path"),
        ("parcels", "p4_parcels_source_path", "p4_parcels_dest_path"),
    )
    for label, source_key, dest_key in pairs:
        source_sr = _describe_sr(arcpy, config[source_key])
        dest_sr = _describe_sr(arcpy, config[dest_key])
        if source_sr == dest_sr:
            log_fn(f"  {label} coordinate system: {source_sr} (source and destination match)")
        else:
            log_fn(
                f"  WARNING: {label} source and destination use different coordinate systems — "
                f"source {source_sr}, destination {dest_sr}. Append reprojects, which shifts the "
                "geometry and can push a feature outside the map's extent filter."
            )


def _preflight_phase4(arcpy, config, log_fn):
    dataset_labels = [
        ("ML source", config["p4_ml_source_path"]),
        ("ML destination", config["p4_ml_dest_path"]),
        ("ML layer (HFML)", config["p4_ml_layer_path"]),
        ("LL source", config["p4_ll_source_path"]),
        ("LL destination", config["p4_ll_dest_path"]),
        ("Parcels source", config["p4_parcels_source_path"]),
        ("Parcels destination", config["p4_parcels_dest_path"]),
    ]
    for label, path in dataset_labels:
        _require_dataset(arcpy, path, label, log_fn)

    writable_targets = [
        ("ML destination", config["p4_ml_dest_path"]),
        ("ML layer (HFML)", config["p4_ml_layer_path"]),
        ("LL destination", config["p4_ll_dest_path"]),
        ("Parcels destination", config["p4_parcels_dest_path"]),
    ]
    for label, path in writable_targets:
        _require_write_lock(arcpy, path, label)
    log_fn("Phase 4 destination write locks available")
    _report_spatial_references(arcpy, config, log_fn)

    ml_source_fields = _get_field_names(arcpy, config["p4_ml_source_path"])
    if "OBJECTID" not in ml_source_fields:
        raise RuntimeError(
            f"ML source is missing OBJECTID, which Phase 4 tracing requires: {config['p4_ml_source_path']}"
        )
    log_fn(f"LL source field count: {len(_get_field_names(arcpy, config['p4_ll_source_path']))}")
    log_fn(f"LL destination field count: {len(_get_field_names(arcpy, config['p4_ll_dest_path']))}")
    log_fn(f"Parcels source field count: {len(_get_field_names(arcpy, config['p4_parcels_source_path']))}")
    log_fn(f"Parcels destination field count: {len(_get_field_names(arcpy, config['p4_parcels_dest_path']))}")


class _StepTimer:
    """Times each step of Phase 4, per HFML and across the run.

    Phase 4 spends minutes per HFML and the log never said where, so a guess
    about what to optimise was only ever a guess.  Read-only: nothing here
    changes what the pipeline does.
    """

    def __init__(self, log_fn):
        self.log_fn = log_fn
        self.run_totals = {}
        self.order = []
        self.current = {}

    @contextmanager
    def step(self, label):
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            self.current[label] = self.current.get(label, 0.0) + elapsed
            self.run_totals[label] = self.run_totals.get(label, 0.0) + elapsed
            if label not in self.order:
                self.order.append(label)

    def start_hfml(self):
        self.current = {}

    def log_hfml(self, pmnum):
        if not self.current:
            return
        total = sum(self.current.values())
        parts = ", ".join(
            f"{label} {seconds:.1f}s"
            for label, seconds in sorted(self.current.items(), key=lambda kv: -kv[1])
        )
        self.log_fn(f"  Timing for {pmnum} — {total:.1f}s total: {parts}")

    def log_run(self):
        if not self.run_totals:
            return
        total = sum(self.run_totals.values())
        self.log_fn(f"Phase 4 time by step ({total:.1f}s across all HFMLs):")
        for label, seconds in sorted(self.run_totals.items(), key=lambda kv: -kv[1]):
            share = (seconds / total * 100) if total else 0
            self.log_fn(f"    {label}: {seconds:.1f}s ({share:.0f}%)")


class _NodeSnapper:
    """Maps pipe endpoints that are close together onto one shared key.

    The trace matches manholes by exact key.  Sewer data is rarely snapped to
    the thousandth of a foot, so two pipe ends 0.01 ft apart used to be two
    different manholes -- the branch above the gap was unreachable and silently
    missing from the map.  An endpoint within `tolerance` of an endpoint already
    seen now takes that one's coordinates.

    Keys stay (x, y) tuples, so everything downstream is unchanged -- including
    the downstream-manhole point Phase 4 hands to the lateral filter.

    Tolerance 0 restores the old behaviour (round to 3 decimals, exact match).
    """

    def __init__(self, tolerance):
        self.tolerance = float(tolerance or 0.0)
        # One grid cell per tolerance, so a 3x3 cell search always covers it.
        self.cell = self.tolerance if self.tolerance > 0 else 1.0
        self._grid = {}
        self.snapped = 0

    def key(self, x, y):
        if self.tolerance <= 0:
            return (round(x, 3), round(y, 3))
        cx = int(math.floor(x / self.cell))
        cy = int(math.floor(y / self.cell))
        best, best_distance = None, None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for rep in self._grid.get((cx + dx, cy + dy), ()):
                    distance = math.hypot(x - rep[0], y - rep[1])
                    if distance <= self.tolerance and (
                        best_distance is None or distance < best_distance
                    ):
                        best, best_distance = rep, distance
        if best is not None:
            # Representatives are stored rounded to 3 decimals, so an end that
            # was already coincident still measures a fraction of a thousandth
            # away.  Only count a real merge.
            if best_distance > 0.001:
                self.snapped += 1
            return best
        rep = (round(x, 3), round(y, 3))
        self._grid.setdefault((cx, cy), []).append(rep)
        return rep


def _build_point_index(items, cell):
    """Grid index over (x, y, payload) tuples, for nearest-point lookups."""
    index = {}
    for x, y, payload in items:
        cx = int(math.floor(x / cell))
        cy = int(math.floor(y / cell))
        index.setdefault((cx, cy), []).append((x, y, payload))
    return index


def _near_points(index, cell, x, y, radius):
    """Indexed points within radius of (x, y), nearest first.

    Each entry is (distance, px, py, payload).
    """
    span = int(math.ceil(radius / cell))
    cx = int(math.floor(x / cell))
    cy = int(math.floor(y / cell))
    found = []
    for dx in range(-span, span + 1):
        for dy in range(-span, span + 1):
            for px, py, payload in index.get((cx + dx, cy + dy), ()):
                distance = math.hypot(px - x, py - y)
                if distance <= radius:
                    found.append((distance, px, py, payload))
    found.sort(key=lambda item: item[0])
    return found


def _endpoint_index(geoms):
    """Index of every pipe end in the network, payload = its OID."""
    def _points():
        for oid, (x1, y1, x2, y2) in geoms.items():
            yield (x1, y1, oid)
            yield (x2, y2, oid)

    return _build_point_index(_points(), GAP_REPORT_RADIUS_FEET)


def _report_network_gaps(node_keys, log_fn):
    """Report how close manholes sit to each other without sharing a key.

    Read-only, and run once when the network cache is built.  A close pair is
    not proof of a gap -- two parallel pipes can legitimately end a few inches
    apart -- but a large 0.01-0.1 ft bucket means the data is not snapped, and
    the shape of this is what should pick p4_node_snap_tolerance_feet.
    """
    coordinate_keys = [key for key in node_keys if isinstance(key, tuple) and len(key) == 2]
    if not coordinate_keys:
        return
    radius = max(GAP_HISTOGRAM_BUCKETS)
    index = _build_point_index(((x, y, None) for x, y in coordinate_keys), radius)
    counts = {bucket: 0 for bucket in GAP_HISTOGRAM_BUCKETS}
    closest = None
    for x, y in coordinate_keys:
        near = [item for item in _near_points(index, radius, x, y, radius) if item[0] > 0]
        if not near:
            continue
        distance = near[0][0]
        for bucket in GAP_HISTOGRAM_BUCKETS:
            if distance <= bucket:
                counts[bucket] += 1
                break
        if closest is None or distance < closest:
            closest = distance
    total = sum(counts.values())
    if not total:
        log_fn(f"  Manhole spacing: none within {radius:g} ft of another — the network looks snapped")
        return
    breakdown = ", ".join(
        f"<={bucket:g} ft: {counts[bucket]}" for bucket in GAP_HISTOGRAM_BUCKETS if counts[bucket]
    )
    log_fn(
        f"  Manhole spacing: {total} of {len(coordinate_keys)} manholes have another "
        f"within {radius:g} ft but not at the same spot ({breakdown}); closest {closest:.4f} ft"
    )
    log_fn(
        "  Those are candidate gaps in the data.  Set p4_node_snap_tolerance_feet "
        "above the bucket holding most of them to close them."
    )


def _build_directed_network(arcpy, ml_source, start_geom, log_fn, snap_tolerance=0.0, report_gaps=False):
    """Build a directed upstream network from the ML source.

    start_geom limits the network to pipes within BUFFER_DISTANCE of that
    geometry.  Pass None to build over the whole source, which is what the
    cached once-per-run build does.

    Direction is determined by (in priority order):
      1. UPNODE / DNNODE node-ID fields if present — explicit and reliable.
      2. Digitizing convention: firstPoint = upstream, lastPoint = downstream.

    Returns:
      directed_net  — dict: downstream_key → [(oid, upstream_key)]
                      "what pipes flow INTO this key"
      geoms         — dict: oid → (x1, y1, x2, y2) rounded coordinates
      pipe_ups      — dict: oid → upstream_key
      pipe_dns      — dict: oid → downstream_key
      use_nodes     — True when keys are node IDs rather than (x, y) coordinates
    """
    if start_geom is None:
        # Whole-extent build, used once per run and then cached.  Nothing is
        # cut off at the buffer edge, so a trace with a long upstream run is
        # followed to its real end rather than to 50,000 feet.
        log_fn("Building the directed network over the full extent of the ML source...")
        _replace_temp_layer(arcpy, "_NetworkSource", ml_source)
    else:
        log_fn(f"Spatially filtering network within {BUFFER_DISTANCE} of target HFML...")
        seed_fc = _geom_to_temp_fc(arcpy, start_geom, "_SeedGeomNet")
        _replace_temp_layer(arcpy, "_NetworkSource", ml_source)
        arcpy.management.SelectLayerByLocation(
            "_NetworkSource", "WITHIN_A_DISTANCE", seed_fc,
            search_distance=BUFFER_DISTANCE,
            selection_type="NEW_SELECTION"
        )
        arcpy.management.Delete(seed_fc)

    # Detect node-ID fields for explicit direction (case-insensitive search)
    _up_aliases = {"upnode", "us_node", "usnode", "fromnode", "node_f"}
    _dn_aliases = {"dnnode", "ds_node", "dsnode", "tonode",   "node_t"}
    all_field_names = {f.name.lower(): f.name for f in arcpy.ListFields("_NetworkSource")}
    upnode_f = next((all_field_names[k] for k in _up_aliases if k in all_field_names), None)
    dnnode_f = next((all_field_names[k] for k in _dn_aliases if k in all_field_names), None)
    use_nodes = bool(upnode_f and dnnode_f)

    if use_nodes:
        log_fn(f"Directed trace using node fields: {upnode_f} (up) / {dnnode_f} (dn)")
    else:
        log_fn("No node-ID fields found — directed trace using digitizing convention (firstPoint=upstream)")

    cursor_fields = ["SHAPE@", "OBJECTID"] + ([upnode_f, dnnode_f] if use_nodes else [])

    snapper = _NodeSnapper(snap_tolerance)
    if snapper.tolerance > 0:
        log_fn(f"Snapping pipe ends within {snapper.tolerance:g} ft onto one manhole")

    directed_net = {}  # dn_key → [(oid, up_key)]
    geoms     = {}     # oid   → (x1, y1, x2, y2)
    pipe_ups  = {}     # oid   → up_key
    pipe_dns  = {}     # oid   → dn_key

    try:
        with arcpy.da.SearchCursor("_NetworkSource", cursor_fields) as cursor:
            for row in cursor:
                geom, oid = row[0], row[1]
                if geom is None:
                    continue
                fp = snapper.key(geom.firstPoint.X, geom.firstPoint.Y)
                lp = snapper.key(geom.lastPoint.X,  geom.lastPoint.Y)
                geoms[oid] = (fp[0], fp[1], lp[0], lp[1])

                if use_nodes:
                    up_key = row[2] if row[2] is not None else fp
                    dn_key = row[3] if row[3] is not None else lp
                else:
                    up_key, dn_key = fp, lp   # digitizing convention

                pipe_ups[oid] = up_key
                pipe_dns[oid] = dn_key
                directed_net.setdefault(dn_key, []).append((oid, up_key))
    finally:
        if arcpy.Exists("_NetworkSource"):
            arcpy.management.Delete("_NetworkSource")

    log_fn(f"Directed network built from {len(geoms)} features ({len(directed_net)} nodes)")
    if snapper.tolerance > 0:
        log_fn(f"  {snapper.snapped} pipe end(s) snapped onto a nearby manhole")
    if report_gaps and not use_nodes:
        node_keys = set(pipe_ups.values()) | set(pipe_dns.values())
        _report_network_gaps(node_keys, log_fn)
    return directed_net, geoms, pipe_ups, pipe_dns, use_nodes


def _against_flow_run(start_oid, outflows, pipe_dns, visited, stop_keys, blocked_keys):
    """start_oid is digitized away from a traced manhole, which already drains
    through the pipe the trace climbed.  A gravity main can't end in nothing,
    so if everything digitized onward from start_oid dead-ends (or runs back
    into the traced area, stop_keys), those pipes are digitized against the
    flow and really drain into the manhole: return them.  Return None for a
    real flow split — the run reaches the HFML's downstream side (blocked_keys)
    or goes on for more than MAX_AGAINST_FLOW_RUN pipes into the wider network.
    """
    run, todo, seen = [start_oid], [pipe_dns[start_oid]], set()
    while todo:
        key = todo.pop()
        if key in seen or key in stop_keys:
            continue
        if key in blocked_keys:
            return None
        seen.add(key)
        for oid in outflows.get(key, []):
            if oid in visited or oid in run:
                continue
            run.append(oid)
            if len(run) > MAX_AGAINST_FLOW_RUN:
                return None
            todo.append(pipe_dns[oid])
    return run


def _downstream_keys(dn_key, outflows, pipe_dns, seeds):
    # Every manhole digitized downstream of the HFML.  None of them can be
    # upstream of it, so the trace never enters them (e.g. through a loop).
    keys, todo = set(), [dn_key]
    while todo:
        key = todo.pop()
        if key in keys:
            continue
        keys.add(key)
        todo.extend(pipe_dns[oid] for oid in outflows.get(key, []) if oid not in seeds)
    return keys


def _hfml_ends(seed_oids, pipe_ups, pipe_dns, outflows, infer_direction, log_fn, directed_net=None):
    """Return (upstream key, downstream key) of the HFML, whose segments
    (seed_oids, longest first) join end to end."""
    counts = {}
    for oid in seed_oids:
        for key in (pipe_ups[oid], pipe_dns[oid]):
            counts[key] = counts.get(key, 0) + 1
    ends = [key for key, n in counts.items() if n == 1]
    dns = {pipe_dns[oid] for oid in seed_oids}
    if len(ends) == 2 and (ends[0] in dns) != (ends[1] in dns):
        up_key, dn_key = (ends[1], ends[0]) if ends[0] in dns else (ends[0], ends[1])
    else:
        # Not one consistently digitized chain: go by the longest segment.
        up_key, dn_key = pipe_ups[seed_oids[0]], pipe_dns[seed_oids[0]]

    if infer_direction:
        seeds = set(seed_oids)
        dn_out = [oid for oid in outflows.get(dn_key, []) if oid not in seeds]
        up_out = [oid for oid in outflows.get(up_key, []) if oid not in seeds]
        # Nothing drains out of the HFML's digitized downstream end, while its
        # upstream end drains into the wider network: the HFML itself is
        # digitized against the flow.
        if not dn_out and any(
            _against_flow_run(oid, outflows, pipe_dns, seeds, set(), set()) is None for oid in up_out
        ):
            log_fn("  NOTE: the HFML appears digitized against the flow — tracing upstream from its other end")
            up_key, dn_key = dn_key, up_key
        elif not dn_out and not up_out and directed_net is not None:
            # Neither end drains anywhere, so outflows cannot say which way the
            # HFML runs and the digitized order is all we had.  Fall back to
            # what arrives: upstream pipes flow INTO the HFML's upstream
            # manhole, so inflows at one end only point that way.
            #
            # This cannot misread a genuine top-of-system HFML: that has a
            # downstream main leaving its downstream end, which puts something
            # in dn_out and stops us reaching here at all.
            dn_in = [oid for oid, _ in directed_net.get(dn_key, []) if oid not in seeds]
            up_in = [oid for oid, _ in directed_net.get(up_key, []) if oid not in seeds]
            if dn_in and not up_in:
                log_fn(
                    "  NOTE: neither end of the HFML drains anywhere, and pipes arrive only at its "
                    "digitized downstream end — treating that end as upstream"
                )
                up_key, dn_key = dn_key, up_key
    return up_key, dn_key


def _report_trace_edge_gaps(queued, visited, directed_net, endpoint_index, log):
    """Report unconnected pipe ends sitting just off the edge of the trace.

    The trace stops wherever nothing more flows into a manhole.  When another
    pipe's end lies a fraction of a foot from that manhole without sharing its
    key, that is a gap in the data, and everything above it is missing from the
    map.  Read-only: nothing here changes what the trace collected.
    """
    if endpoint_index is None:
        return
    gaps = []
    for key in queued:
        if not (isinstance(key, tuple) and len(key) == 2):
            continue  # node-ID keys carry no coordinates
        if any(oid not in visited for oid, _ in directed_net.get(key, [])):
            continue  # not an edge of the trace
        for distance, px, py, oid in _near_points(
            endpoint_index, GAP_REPORT_RADIUS_FEET, key[0], key[1], GAP_REPORT_RADIUS_FEET
        ):
            if distance <= 0 or oid in visited:
                continue
            gaps.append((distance, key, oid, (round(px, 4), round(py, 4))))
            break
    if not gaps:
        return
    gaps.sort(key=lambda item: item[0])
    log(
        f"  NOTE: {len(gaps)} edge(s) of this trace have an unconnected pipe end within "
        f"{GAP_REPORT_RADIUS_FEET:g} ft. Each looks like a gap in the data, and the branch above "
        "it is missing from this map:"
    )
    for distance, key, oid, point in gaps[:MAX_GAP_REPORT_LINES]:
        log(f"    manhole {key} — OID {oid} ends at {point}, {distance:.4f} ft away")
    if len(gaps) > MAX_GAP_REPORT_LINES:
        log(f"    ... and {len(gaps) - MAX_GAP_REPORT_LINES} more")
    log(
        "  Raising p4_node_snap_tolerance_feet above those distances would close them "
        "(it also merges manholes that really are that close, so raise it no further than needed)."
    )


def _trace_upstream_directed(
    seed_oids, directed_net, pipe_ups, pipe_dns, infer_direction=True, log_fn=None,
    endpoint_index=None,
):
    """DFS upstream from the HFML (seed_oids: its segments, longest first).
    Returns (upstream OIDs, the HFML's downstream key).

    Pipes flowing into a traced manhole are followed.  With infer_direction
    (keys are coordinates, so direction comes from digitizing), pipes digitized
    against the flow are followed too (see _against_flow_run), and an HFML
    digitized against the flow is traced from its other end.

    Every map is cleaned up after export, so pipes already present in the
    destination (e.g. from older runs) are traced through like any other.
    """
    log = log_fn or (lambda msg: None)
    outflows = {}
    for oid, key in pipe_ups.items():
        outflows.setdefault(key, []).append(oid)

    seeds = set(seed_oids)
    up_key, dn_key = _hfml_ends(
        seed_oids, pipe_ups, pipe_dns, outflows, infer_direction, log, directed_net=directed_net,
    )
    blocked = _downstream_keys(dn_key, outflows, pipe_dns, seeds)
    # Every manhole along the HFML except its downstream end: side mains can
    # join an HFML that is split into several segments part-way along.
    start_keys = ({pipe_ups[oid] for oid in seeds} | {pipe_dns[oid] for oid in seeds}) - {dn_key}
    # If nothing flows out of the HFML's downstream end (a lift station, or a
    # pipe there digitized against the flow) its direction is unconfirmed, and
    # a pipe leaving its upstream end could be the real downstream main, so
    # don't follow those.
    confirmed = any(oid not in seeds for oid in outflows.get(dn_key, []))

    visited = set(seeds)
    to_append, against_flow, looped = [], [], []
    stack, queued = list(start_keys), set(start_keys)
    while stack:
        key = stack.pop()
        for oid, from_key in directed_net.get(key, []):
            if oid in visited:
                continue
            if from_key in blocked:
                looped.append(oid)
                continue
            visited.add(oid)
            to_append.append(oid)
            if from_key not in queued:
                queued.add(from_key)
                stack.append(from_key)
        if not infer_direction or (key in start_keys and not confirmed):
            continue
        for oid in outflows.get(key, []):
            if oid in visited:
                continue
            for run_oid in _against_flow_run(oid, outflows, pipe_dns, visited, queued, blocked) or []:
                visited.add(run_oid)
                to_append.append(run_oid)
                against_flow.append(run_oid)
                if pipe_dns[run_oid] not in queued:
                    queued.add(pipe_dns[run_oid])
                    stack.append(pipe_dns[run_oid])

    log(f"Upstream trace found {len(to_append)} pipes from the HFML's upstream end {up_key!r}")
    _report_trace_edge_gaps(queued, visited, directed_net, endpoint_index, log)
    if against_flow:
        log(
            f"  Followed {len(against_flow)} pipe(s) digitized against the flow (they lead only to "
            f"dead ends or back into the trace): {_preview_oids(sorted(against_flow))}"
        )
    looped = sorted(set(looped) - visited)
    if looped:
        log(
            f"  NOTE: {len(looped)} pipe(s) run from the HFML's downstream side into a traced manhole "
            f"and were not followed (a loop or reversed duplicate): {_preview_oids(looped)}"
        )
    # Pipes that START at a traced manhole and weren't followed: real flow
    # splits, or runs digitized against the flow that join the wider network —
    # list them so a missing branch can be checked.
    outflow = sorted({oid for key in queued for oid in outflows.get(key, []) if oid not in visited})
    if outflow:
        log(
            f"  NOTE: {len(outflow)} pipe(s) start at a traced manhole and were not followed "
            f"(a flow split, or digitized against the flow beyond what the trace can infer): "
            f"{_preview_oids(outflow)}"
        )
    return to_append, dn_key


def _append_by_oids(arcpy, oid_list, source, destination):
    if not oid_list:
        return
    query = f"OBJECTID IN ({','.join(map(str, oid_list))})"
    subset_layer = _replace_temp_layer(arcpy, "_TmpSubset", source, query)
    try:
        _with_arcpy_retry(
            arcpy,
            lambda: arcpy.management.Append(subset_layer, destination, "NO_TEST"),
            f"Failed to append from {source} to {destination}",
        )
    finally:
        if arcpy.Exists(subset_layer):
            arcpy.management.Delete(subset_layer)


def _preview_oids(oid_list, limit=10):
    if not oid_list:
        return "[]"
    preview = ", ".join(map(str, oid_list[:limit]))
    if len(oid_list) > limit:
        preview += ", ..."
    return f"[{preview}]"


def _get_max_oid(arcpy, path):
    max_oid = 0
    with arcpy.da.SearchCursor(path, ["OBJECTID"]) as cur:
        for (oid,) in cur:
            if oid > max_oid:
                max_oid = oid
    return max_oid


def _snapshot_destinations(arcpy, dest_paths):
    return {path: _get_max_oid(arcpy, path) for path in dest_paths}


def _delete_rows_after(arcpy, path, max_oid):
    def _delete():
        deleted = 0
        with arcpy.da.UpdateCursor(path, ["OBJECTID"], f"OBJECTID > {max_oid}") as cur:
            for _ in cur:
                cur.deleteRow()
                deleted += 1
        return deleted

    return _with_arcpy_retry(arcpy, _delete, f"Failed to clean up appended rows in {path}")


def _cleanup_destinations(arcpy, snapshot, log_fn):
    """Remove every row appended since the snapshot so the destinations look
    as if this HFML was never traced.  The exported PDF is the only output."""
    leftovers = []
    for path, max_oid in snapshot.items():
        try:
            deleted = _delete_rows_after(arcpy, path, max_oid)
            log_fn(f"  Cleaned up {deleted} row(s) from {path}")
        except Exception as exc:
            log_fn(f"  ERROR: cleanup failed for {path} — {exc}")
            leftovers.append(f"{path} (OBJECTID > {max_oid})")
    if leftovers:
        # Stop rather than carry on: leftover rows would be drawn on later maps,
        # and a leftover HFML seed makes its pmnum count as already mapped.
        raise RuntimeError(
            "Could not remove the rows appended for this HFML from: "
            + "; ".join(leftovers)
            + ". Delete those rows by hand, then rerun Phase 4."
        )


def _write_phase4_map_manifest(hfml_records, log_fn):
    manifest = {"version": 1, "source": "phase4", "hfmls": hfml_records}
    os.makedirs(os.path.dirname(PHASE4_MAP_OUTPUT_PATH), exist_ok=True)
    with open(PHASE4_MAP_OUTPUT_PATH, "w") as f:
        json.dump(manifest, f, indent=2)
    log_fn(f"Phase 4 map manifest written: {PHASE4_MAP_OUTPUT_PATH}")


def _build_ll_endpoint_table(arcpy, ll_layer, output_name="_TmpLLEndpoints"):
    output_path = f"in_memory\\{output_name}"
    if arcpy.Exists(output_path):
        arcpy.management.Delete(output_path)

    ll_desc = arcpy.Describe(ll_layer)
    spatial_ref = ll_desc.spatialReference
    arcpy.management.CreateFeatureclass("in_memory", output_name, "POINT", spatial_reference=spatial_ref)
    arcpy.management.AddField(output_path, "LL_OID", "LONG")
    arcpy.management.AddField(output_path, "END_ID", "LONG")

    end_id = 1
    with arcpy.da.InsertCursor(output_path, ["SHAPE@", "LL_OID", "END_ID"]) as insert_cursor:
        with arcpy.da.SearchCursor(ll_layer, ["OBJECTID", "SHAPE@"]) as search_cursor:
            for ll_oid, geom in search_cursor:
                if geom is None:
                    continue
                if geom.firstPoint is not None:
                    insert_cursor.insertRow([arcpy.PointGeometry(geom.firstPoint, spatial_ref), ll_oid, end_id])
                    end_id += 1
                if geom.lastPoint is not None:
                    insert_cursor.insertRow([arcpy.PointGeometry(geom.lastPoint, spatial_ref), ll_oid, end_id])
                    end_id += 1

    return output_path


def _get_count(arcpy, layer_or_table):
    return int(arcpy.management.GetCount(layer_or_table)[0])


def _select_lls_contacting_mls(
    arcpy, ml_oid_list, ml_source, ll_source, seed_ml_layer=None, downstream_point=None, log_fn=None,
):
    if not ml_oid_list and seed_ml_layer is None:
        return []

    ml_layer = None
    if ml_oid_list:
        query = f"OBJECTID IN ({','.join(map(str, ml_oid_list))})"
        ml_layer = _replace_temp_layer(arcpy, "_TmpML", ml_source, query)
    ll_layer = _replace_temp_layer(arcpy, "_TmpLL", ll_source)
    candidate_ll_layer = None
    endpoint_fc = None
    endpoint_layer = None
    try:
        selection_made = False
        if ml_layer is not None:
            arcpy.management.SelectLayerByLocation(
                ll_layer,
                "WITHIN_A_DISTANCE",
                ml_layer,
                search_distance=LL_SEARCH_DISTANCE,
                selection_type="NEW_SELECTION",
            )
            selection_made = True
        if seed_ml_layer is not None:
            arcpy.management.SelectLayerByLocation(
                ll_layer,
                "WITHIN_A_DISTANCE",
                seed_ml_layer,
                search_distance=LL_SEARCH_DISTANCE,
                selection_type="ADD_TO_SELECTION" if selection_made else "NEW_SELECTION",
            )
            selection_made = True

        if not selection_made:
            return []

        candidate_ll_layer = _replace_temp_layer(arcpy, "_TmpLLCandidates", ll_layer)
        if log_fn:
            log_fn(f"Candidate LL count before endpoint filter: {_get_count(arcpy, candidate_ll_layer)}")

        endpoint_fc = _build_ll_endpoint_table(arcpy, candidate_ll_layer)
        endpoint_layer = _replace_temp_layer(arcpy, "_TmpLLEndpointLayer", endpoint_fc)
        arcpy.management.SelectLayerByAttribute(endpoint_layer, "CLEAR_SELECTION")

        endpoint_selection_made = False
        if ml_layer is not None:
            arcpy.management.SelectLayerByLocation(
                endpoint_layer,
                "WITHIN_A_DISTANCE",
                ml_layer,
                search_distance=LL_SEARCH_DISTANCE,
                selection_type="NEW_SELECTION",
            )
            endpoint_selection_made = True
        if seed_ml_layer is not None:
            arcpy.management.SelectLayerByLocation(
                endpoint_layer,
                "WITHIN_A_DISTANCE",
                seed_ml_layer,
                search_distance=LL_SEARCH_DISTANCE,
                selection_type="ADD_TO_SELECTION" if endpoint_selection_made else "NEW_SELECTION",
            )

        if log_fn:
            log_fn(f"LL endpoint hits after {LL_SEARCH_DISTANCE} filter: {_get_count(arcpy, endpoint_layer)}")

        hits = {}
        with arcpy.da.SearchCursor(endpoint_layer, ["LL_OID", "SHAPE@XY"]) as cursor:
            for ll_oid, (x, y) in cursor:
                hits.setdefault(ll_oid, []).append((x, y))

        # An LL whose only contact with the traced MLs is the HFML's downstream
        # manhole drains downstream of the HFML, so it is not part of the trace.
        if downstream_point is not None:
            dn_x, dn_y = downstream_point
            at_downstream = [
                ll_oid for ll_oid, points in hits.items()
                if all(math.hypot(x - dn_x, y - dn_y) <= DOWNSTREAM_MANHOLE_TOLERANCE for x, y in points)
            ]
            for ll_oid in at_downstream:
                del hits[ll_oid]
            if log_fn and at_downstream:
                log_fn(f"Excluded {len(at_downstream)} LL(s) tied in only at the HFML's downstream manhole")

        return sorted(hits)
    finally:
        if ml_layer is not None and arcpy.Exists(ml_layer):
            arcpy.management.Delete(ml_layer)
        if arcpy.Exists(ll_layer):
            arcpy.management.Delete(ll_layer)
        if candidate_ll_layer is not None and arcpy.Exists(candidate_ll_layer):
            arcpy.management.Delete(candidate_ll_layer)
        if endpoint_layer is not None and arcpy.Exists(endpoint_layer):
            arcpy.management.Delete(endpoint_layer)
        if endpoint_fc is not None and arcpy.Exists(endpoint_fc):
            arcpy.management.Delete(endpoint_fc)




def _copy_endpoint_subset(arcpy, source_layer, endpoint_ids, output_name="_TmpParcelSideEndpoints"):
    output_path = f"in_memory\\{output_name}"
    if arcpy.Exists(output_path):
        arcpy.management.Delete(output_path)

    source_desc = arcpy.Describe(source_layer)
    spatial_ref = source_desc.spatialReference
    arcpy.management.CreateFeatureclass("in_memory", output_name, "POINT", spatial_reference=spatial_ref)

    endpoint_id_set = set(endpoint_ids)
    with arcpy.da.InsertCursor(output_path, ["SHAPE@"]) as insert_cursor:
        with arcpy.da.SearchCursor(source_layer, ["SHAPE@", "END_ID"]) as search_cursor:
            for geom, end_id in search_cursor:
                if end_id in endpoint_id_set:
                    insert_cursor.insertRow([geom])

    return output_path


def _select_parcels_intersecting_lls(
    arcpy,
    ll_oid_list,
    ll_source,
    parcels_source,
    ml_oid_list=None,
    ml_source=None,
    seed_ml_layer=None,
    log_fn=None,
):
    if not ll_oid_list:
        return []
    query = f"OBJECTID IN ({','.join(map(str, ll_oid_list))})"
    ll_layer = _replace_temp_layer(arcpy, "_TmpLL", ll_source, query)
    parcels_layer = _replace_temp_layer(arcpy, "_TmpParcels", parcels_source)
    ml_layer = None
    endpoint_fc = None
    endpoint_layer = None
    parcel_endpoint_fc = None
    parcel_endpoint_layer = None
    try:
        endpoint_fc = _build_ll_endpoint_table(arcpy, ll_layer, "_TmpParcelLLEndpoints")
        endpoint_layer = _replace_temp_layer(arcpy, "_TmpParcelLLEndpointLayer", endpoint_fc)

        # Build per-LL endpoint mapping before any selection is active.
        ll_to_end_ids = {}
        endpoint_ids  = set()
        with arcpy.da.SearchCursor(endpoint_layer, ["END_ID", "LL_OID"]) as _cur:
            for _end_id, _ll_oid in _cur:
                endpoint_ids.add(_end_id)
                ll_to_end_ids.setdefault(_ll_oid, []).append(_end_id)
        ml_endpoint_ids = set()
        selection_made = False

        if ml_oid_list and ml_source:
            ml_query = f"OBJECTID IN ({','.join(map(str, ml_oid_list))})"
            ml_layer = _replace_temp_layer(arcpy, "_TmpParcelML", ml_source, ml_query)
            arcpy.management.SelectLayerByLocation(
                endpoint_layer,
                "WITHIN_A_DISTANCE",
                ml_layer,
                search_distance=LL_SEARCH_DISTANCE,
                selection_type="NEW_SELECTION",
            )
            selection_made = True

        if seed_ml_layer is not None:
            arcpy.management.SelectLayerByLocation(
                endpoint_layer,
                "WITHIN_A_DISTANCE",
                seed_ml_layer,
                search_distance=LL_SEARCH_DISTANCE,
                selection_type="ADD_TO_SELECTION" if selection_made else "NEW_SELECTION",
            )
            selection_made = True

        if selection_made:
            ml_endpoint_ids = {row[0] for row in arcpy.da.SearchCursor(endpoint_layer, ["END_ID"])}

        # Per-LL classification: for each LL that has at least one ML-side
        # endpoint, its parcel-side endpoint is the OTHER one.  Global set
        # subtraction would incorrectly exclude parcel-side endpoints that
        # happen to also lie on a ML boundary.
        parcel_endpoint_ids = set()
        for _ll_oid, _end_ids in ll_to_end_ids.items():
            _ml_side     = [e for e in _end_ids if e in ml_endpoint_ids]
            if not _ml_side:
                continue  # LL has no ML connection — skip
            _parcel_side = [e for e in _end_ids if e not in ml_endpoint_ids]
            if _parcel_side:
                parcel_endpoint_ids.update(_parcel_side)
            else:
                # Both endpoints are within ML search distance (very short LL).
                # Include all and let the spatial parcel selection decide.
                parcel_endpoint_ids.update(_end_ids)
        if log_fn:
            log_fn(f"LL parcel-side endpoint count: {len(parcel_endpoint_ids)}")

        if not parcel_endpoint_ids:
            return []

        arcpy.management.SelectLayerByAttribute(endpoint_layer, "CLEAR_SELECTION")
        parcel_endpoint_fc = _copy_endpoint_subset(arcpy, endpoint_layer, parcel_endpoint_ids)
        parcel_endpoint_layer = _replace_temp_layer(
            arcpy,
            "_TmpParcelSideEndpointLayer",
            parcel_endpoint_fc,
        )

        # Each parcel-side end takes the parcel(s) it lies in or on (a shared
        # edge gives both); an end that lies in no parcel takes the nearest one
        # within PARCEL_SEARCH_FEET.
        arcpy.management.SelectLayerByLocation(
            parcels_layer, "WITHIN_A_DISTANCE", parcel_endpoint_layer,
            search_distance=f"{PARCEL_SEARCH_FEET} Feet", selection_type="NEW_SELECTION",
        )
        with arcpy.da.SearchCursor(parcels_layer, ["OBJECTID", "SHAPE@"]) as cursor:
            candidates = [(oid, geom, geom.extent) for oid, geom in cursor if geom is not None]
        chosen, nearest_used, unmatched = set(), [], 0
        tol = PARCEL_SEARCH_FEET
        with arcpy.da.SearchCursor(parcel_endpoint_layer, ["SHAPE@"]) as cursor:
            for (end,) in cursor:
                pt = end.firstPoint
                dists = sorted(
                    (geom.distanceTo(end), oid) for oid, geom, ext in candidates
                    if ext.XMin - tol <= pt.X <= ext.XMax + tol and ext.YMin - tol <= pt.Y <= ext.YMax + tol
                )
                dists = [(d, oid) for d, oid in dists if d <= tol]
                if not dists:
                    unmatched += 1
                elif dists[0][0] <= 0.001:
                    chosen.update(oid for d, oid in dists if d <= 0.001)
                else:
                    chosen.add(dists[0][1])
                    nearest_used.append(dists[0][0])
        if log_fn and nearest_used:
            log_fn(
                f"  {len(nearest_used)} LL end(s) lie in no parcel — used the nearest parcel "
                f"(up to {max(nearest_used):.1f} ft away)"
            )
        if log_fn and unmatched:
            log_fn(f"  {unmatched} LL end(s) have no parcel within {tol:g} ft")
        return sorted(chosen)
    finally:
        if ml_layer is not None and arcpy.Exists(ml_layer):
            arcpy.management.Delete(ml_layer)
        if arcpy.Exists(ll_layer):
            arcpy.management.Delete(ll_layer)
        if arcpy.Exists(parcels_layer):
            arcpy.management.Delete(parcels_layer)
        if endpoint_layer is not None and arcpy.Exists(endpoint_layer):
            arcpy.management.Delete(endpoint_layer)
        if endpoint_fc is not None and arcpy.Exists(endpoint_fc):
            arcpy.management.Delete(endpoint_fc)
        if parcel_endpoint_layer is not None and arcpy.Exists(parcel_endpoint_layer):
            arcpy.management.Delete(parcel_endpoint_layer)
        if parcel_endpoint_fc is not None and arcpy.Exists(parcel_endpoint_fc):
            arcpy.management.Delete(parcel_endpoint_fc)


def _append_seed_to_hfml_layer(arcpy, seed_oids, ml_source, hfml_layer, pmnum, pm_mainlines_path, log_fn):
    pmnum_field = "PM_Mainlines_pmnum"
    layer_fields = _get_field_names(arcpy, hfml_layer)
    has_pmnum_field = pmnum_field in layer_fields

    # Skip if this pmnum is already in the layer (re-run protection).
    if has_pmnum_field:
        existing = [r[0] for r in arcpy.da.SearchCursor(hfml_layer, [pmnum_field])]
        if pmnum in existing:
            log_fn(f"  HFML seed for pmnum {pmnum} already in layer — skipping")
            return

    max_oid = _get_max_oid(arcpy, hfml_layer)

    _append_by_oids(arcpy, list(seed_oids), ml_source, hfml_layer)

    if not has_pmnum_field:
        log_fn(f"  HFML seed appended for pmnum {pmnum} ('{pmnum_field}' not found — attributes not stamped)")
        return

    # Stamp pmnum and copy the PM_Mainlines attributes (frequency etc.) onto the
    # seed so the layer's symbology classifies it.  HFML layer fields are matched
    # as PM_Mainlines_<field> (joined export naming) or a bare 'frequency'.
    layer_field_info = {f.name.lower(): f for f in arcpy.ListFields(hfml_layer)}
    pm_values = {}
    try:
        pm_fields = [
            f.name for f in arcpy.ListFields(pm_mainlines_path)
            if f.type not in ("OID", "Geometry", "GlobalID", "Blob", "Raster")
        ]
        with arcpy.da.SearchCursor(pm_mainlines_path, pm_fields, f"pmnum = '{pmnum}'") as cur:
            for row in cur:
                pm_values = dict(zip(pm_fields, row))
                break
    except Exception as exc:
        log_fn(f"  WARNING: could not read PM_Mainlines attributes for pmnum {pmnum} — {exc}")

    copies = {}
    for pm_field, value in pm_values.items():
        for candidate in (f"pm_mainlines_{pm_field.lower()}",) + (("frequency",) if pm_field.lower() == "frequency" else ()):
            layer_field = layer_field_info.get(candidate)
            if layer_field is not None and layer_field.name != pmnum_field and layer_field.editable:
                copies[layer_field.name] = _coerce_for_field(value, layer_field.type)
    copies[pmnum_field] = _coerce_for_field(pmnum, layer_field_info[pmnum_field.lower()].type)

    update_fields = ["OBJECTID"] + list(copies)
    updated = 0
    with arcpy.da.UpdateCursor(hfml_layer, update_fields) as cur:
        for row in cur:
            if row[0] > max_oid:
                cur.updateRow([row[0]] + list(copies.values()))
                updated += 1

    freq_fields = [f for f in copies if "frequency" in f.lower()]
    if freq_fields:
        freq_note = ", ".join(f"{f}={copies[f]!r}" for f in freq_fields)
    elif not pm_values:
        freq_note = "attributes NOT copied — pmnum not found in PM_Mainlines"
    else:
        freq_note = "frequency NOT stamped — HFML layer has no PM_Mainlines_frequency/frequency field"
    log_fn(
        f"  HFML seed appended for pmnum {pmnum}, {freq_note} "
        f"({len(copies)} field(s) copied, {updated} row(s))"
    )


def _coerce_for_field(value, field_type):
    """Convert a value to suit the destination field, so e.g. a frequency of
    6.0 is written as '6' to a text field and matches a symbology class '6'."""
    if value is None:
        return None
    try:
        if field_type == "String":
            if isinstance(value, float) and value.is_integer():
                return str(int(value))
            return str(value)
        if field_type in ("Integer", "SmallInteger", "BigInteger"):
            return int(float(value))
        if field_type in ("Double", "Single"):
            return float(value)
    except (TypeError, ValueError):
        return None
    return value


def _asset_chain(segments):
    """Split one asset's GIS segments [(oid, geom)] into the HFML and the rest.

    Segments joined end to end form a group; the HFML is the group with the
    most segments, then the most length (pieces of one asset don't join up by
    accident, while a stray copy of the asset number is usually a lone pipe),
    longest segment first.  The rest are the other groups and exact copies of
    a segment.
    """
    def ends(geom):
        return frozenset({
            (round(geom.firstPoint.X, 3), round(geom.firstPoint.Y, 3)),
            (round(geom.lastPoint.X, 3), round(geom.lastPoint.Y, 3)),
        })

    remaining = sorted(segments, key=lambda seg: seg[1].length, reverse=True)
    groups, copies = [], []
    while remaining:
        group = [remaining.pop(0)]
        group_ends = set(ends(group[0][1]))
        seen_ends = {ends(group[0][1])}
        grown = True
        while grown:
            grown = False
            for seg in list(remaining):
                seg_ends = ends(seg[1])
                if not seg_ends & group_ends:
                    continue
                remaining.remove(seg)
                if seg_ends in seen_ends:
                    copies.append(seg)
                    continue
                group.append(seg)
                group_ends |= seg_ends
                seen_ends.add(seg_ends)
                grown = True
        groups.append(group)
    best = max(groups, key=lambda g: (len(g), sum(seg[1].length for seg in g)))
    return best, [seg for g in groups if g is not best for seg in g] + copies


def _assetnum_where_clause(arcpy, ml_source, assetnums):
    """A WHERE clause selecting only these assetnums, or None to read every row.

    PM_Mainlines, the HFML layer and the ML source do not agree on field type,
    so one assetnum can be stored as 500123, 500123.0 or "500123 " (this is what
    _norm_id exists for).  For a text field both plain and ".0" spellings are
    listed; for a numeric field the numeric form is used.  Anything that cannot
    be expressed safely returns None, and the caller then reads every row as it
    did before -- a slower scan is always better than a silently missed HFML.
    """
    if not assetnums:
        return None
    try:
        field = next(
            (f for f in arcpy.ListFields(ml_source) if f.name.lower() == "mxassetnum"),
            None,
        )
        if field is None:
            return None
        delimited = arcpy.AddFieldDelimiters(ml_source, field.name)
        if field.type in ("String", "Guid"):
            spellings = set()
            for assetnum in assetnums:
                spellings.add(str(assetnum))
                spellings.add(f"{assetnum}.0")
            literals = ", ".join(
                "'" + value.replace("'", "''") + "'" for value in sorted(spellings)
            )
        else:
            numbers = set()
            for assetnum in assetnums:
                try:
                    numbers.add(str(int(float(assetnum))))
                except (TypeError, ValueError):
                    # Not expressible as a number against a numeric field.
                    return None
            literals = ", ".join(sorted(numbers))
        if not literals:
            return None
        clause = f"{delimited} IN ({literals})"
        if len(clause) > MAX_WHERE_CLAUSE_CHARS:
            return None
        return clause
    except Exception:
        return None


def _read_assetnum_segments(arcpy, ml_source, assetnum_set, config, log_fn):
    """Map each wanted assetnum to its (oid, geometry) segments in the ML source.

    Asks the source for just those assetnums when it can.  If that returns
    nothing at all while assetnums were wanted, the clause is assumed wrong for
    how this source stores them and every row is read instead.
    """
    def _scan(where):
        found = {}
        with arcpy.da.SearchCursor(
            ml_source, ["MXASSETNUM", "SHAPE@", "OID@"], where
        ) as cursor:
            for row in cursor:
                assetnum = _norm_id(row[0])
                if assetnum in assetnum_set and row[1] is not None:
                    found.setdefault(assetnum, []).append((row[2], row[1]))
        return found

    where = None
    if config.get("p4_filter_assetnum_scan", True):
        where = _assetnum_where_clause(arcpy, ml_source, assetnum_set)
        if where is None:
            log_fn("  Reading the whole ML source (no safe assetnum filter available)")
        else:
            log_fn(f"  Asking the ML source for {len(assetnum_set)} assetnum(s) only")

    segments = _scan(where)
    if where is not None and not segments and assetnum_set:
        log_fn(
            "  WARNING: the assetnum filter matched nothing — falling back to a "
            "full scan of the ML source"
        )
        segments = _scan(None)
    return segments


def _setup_phase4_cache(arcpy, config, ml_source, log_fn):
    """Prepare the local cache.

    Returns (ml_source_to_use, directed_network_or_None, extra_source_overrides).
    extra_source_overrides maps config keys to local copies of the lateral and
    parcel sources, for the caller to apply.  The network is None
    when caching is off or unusable, and Phase 4 then builds a spatially
    filtered network per HFML exactly as it used to.

    Note the returned path may be a LOCAL COPY of the ML source, whose
    OBJECTIDs differ from the remote original.  Every later step that takes ML
    object IDs -- tracing, appending, and the map export subprocess -- must use
    the returned path.
    """
    if not config.get("p4_cache_enabled", True):
        log_fn("Phase 4 cache disabled in config.json — building the network per HFML")
        return ml_source, None, {}

    cache_dir = config.get("p4_cache_dir") or ""
    if cache_dir and not os.path.isabs(cache_dir):
        log_fn(
            f"  WARNING: p4_cache_dir must be an absolute local path — ignoring {cache_dir!r}"
        )
        cache_dir = ""
    extra_sources = {}
    cache = Phase4Cache(
        cache_dir or default_cache_dir(),
        config.get("p4_cache_max_age_days", 7),
        log_fn,
    )
    log_fn(f"Phase 4 cache folder: {cache.dir}")

    signature = dataset_signature(arcpy, ml_source)
    # Part of the fingerprint: a pickled network built at a different tolerance
    # has different manhole keys and must not be reused.
    signature["snap_tolerance"] = float(config.get("p4_node_snap_tolerance_feet", 0.1) or 0.0)
    log_fn(f"  ML source fingerprint: {signature}")
    if signature_is_weak(signature):
        log_fn(
            "  NOTE: only a feature count is available for this source, so an edit "
            "that leaves the count unchanged would go unnoticed; the cache age "
            "limit is the backstop."
        )

    min_free_gb = float(config.get("p4_cache_min_free_gb", 20) or 0)
    available = free_gb(cache.dir)
    if available is not None:
        log_fn(f"  Cache disk: {available:.1f} GB free")

    effective = ml_source
    if config.get("p4_cache_local_ml_copy", True):
        local = cache.local_ml_copy(arcpy, ml_source, signature, min_free_gb=min_free_gb)
        if local:
            effective = local

    # The lateral and parcel sources are copied the same way.  They carry no
    # keys across runs, but their object IDs are renumbered by the copy just as
    # the ML source's are, so whatever this returns has to be used for the
    # selects, the appends AND the map export (see source_overrides).
    for config_key, name, label, flag in (
        ("p4_ll_source_path", "ll_source", "LL", "p4_cache_local_ll_copy"),
        ("p4_parcels_source_path", "parcels_source", "parcels", "p4_cache_local_parcels_copy"),
    ):
        if not config.get(flag, True):
            continue
        remote = config[config_key]
        local = cache.local_copy(
            arcpy, remote, name, label,
            dataset_signature(arcpy, remote),
            min_free_gb=min_free_gb,
        )
        if local:
            extra_sources[config_key] = local

    network = cache.load_network(effective, signature)
    if network is None:
        started = time.time()
        try:
            network = _build_directed_network(
                arcpy, effective, None, log_fn,
                snap_tolerance=signature["snap_tolerance"],
                report_gaps=bool(config.get("p4_report_network_gaps", True)),
            )
        except Exception as exc:
            log_fn(f"  WARNING: whole-extent network build failed — {exc}")
            log_fn("  Falling back to the per-HFML filtered build.")
            return effective, None, extra_sources
        log_fn(f"  Built the directed network in {time.time() - started:.1f}s")
        cache.save_network(effective, signature, network)
    return effective, network, extra_sources


def run(log_fn):
    # Imported here: phase5 imports PHASE4_MAP_OUTPUT_PATH from this module.
    from backend.phase5.run import (
        export_hfml_map_out_of_process, find_mapped_pdfs, new_run_dir, resolve_project_path,
        run_output_paths,
    )

    log_fn(f"Phase 4 interpreter: {sys.executable}")
    arcpy = _import_arcpy()

    config = load_config()
    ml_source      = config["p4_ml_source_path"]
    ml_dest        = config["p4_ml_dest_path"]
    ll_source      = config["p4_ll_source_path"]
    ll_dest        = config["p4_ll_dest_path"]
    parcels_source = config["p4_parcels_source_path"]
    parcels_dest   = config["p4_parcels_dest_path"]

    product_info = arcpy.ProductInfo()
    if product_info in {"NotInitialized", "Unavailable", "Engine", "ArcServer"}:
        raise RuntimeError(f"ArcGIS Pro license not available: {product_info}")
    log_fn(f"ArcGIS product license: {product_info}")

    _preflight_phase4(arcpy, config, log_fn)
    # Resolve the map project up front so a bad path fails before any tracing.
    aprx_path = resolve_project_path(config, log_fn)
    log_fn(f"Map project: {aprx_path}")

    hfml_layer = config["p4_ml_layer_path"]

    # Collect all HFMLs from PM_Mainlines
    pm_mainlines_path = config["feature_class_path"]
    all_assetnums = {}
    with arcpy.da.SearchCursor(pm_mainlines_path, [config["unique_id_field"], "assetnum"]) as cursor:
        for row in cursor:
            if row[1] is not None:
                all_assetnums[str(row[0])] = _norm_id(row[1])

    log_fn(f"Found {len(all_assetnums)} total HFMLs in PM_Mainlines")

    # Determine which pmnums are already done.  Each HFML's features are deleted
    # after its map is exported, so an existing PDF is what marks it as mapped.
    # Seeds left in PMs_SSGRAVITYMAIN by earlier runs also count.
    done_ids = {_norm_id(p) for p in find_mapped_pdfs(config)}
    try:
        with arcpy.da.SearchCursor(hfml_layer, ["PM_Mainlines_pmnum"]) as cursor:
            for (pmnum,) in cursor:
                if pmnum is not None:
                    done_ids.add(_norm_id(pmnum))
    except Exception as exc:
        log_fn(f"  WARNING: could not read existing seeds from HFML layer — {exc}")

    already_done = {p for p in all_assetnums if _norm_id(p) in done_ids}
    new_assetnums = {p: a for p, a in all_assetnums.items() if p not in already_done}
    log_fn(f"Already processed: {len(already_done)}, New to process: {len(new_assetnums)}")

    if not new_assetnums:
        log_fn("No new HFMLs to process — Phase 4 complete")
        _write_phase4_map_manifest([], log_fn)
        return

    # Local copy of the ML source plus the directed network, both reused until
    # the remote source changes.  ml_source becomes the local copy when one was
    # made, so everything below -- tracing, appends, and the map export -- works
    # from one consistent set of object IDs.
    timer = _StepTimer(log_fn)
    ml_source_remote = ml_source
    with timer.step("cache setup (local source copies + network)"):
        ml_source, cached_network, extra_sources = _setup_phase4_cache(
            arcpy, config, ml_source, log_fn
        )

    # Everything from here on -- selects, appends, and the map export -- must
    # read whichever copy of each source the cache handed back, because a copy
    # renumbers OBJECTID and this code carries object IDs between those steps.
    source_overrides = {}
    if ml_source != ml_source_remote:
        source_overrides["p4_ml_source_path"] = ml_source
    source_overrides.update(extra_sources)
    ll_source = source_overrides.get("p4_ll_source_path", ll_source)
    parcels_source = source_overrides.get("p4_parcels_source_path", parcels_source)
    for key, path in sorted(source_overrides.items()):
        log_fn(f"Using local copy for {key}: {path}")

    assetnum_set = set(new_assetnums.values())
    with timer.step("assetnum lookup"):
        assetnum_segments = _read_assetnum_segments(
            arcpy, ml_source, assetnum_set, config, log_fn
        )

    log_fn(f"Matched {len(assetnum_segments)}/{len(assetnum_set)} assetnums to geometry in ML source")

    new_hfmls = []
    for pmnum, assetnum in new_assetnums.items():
        segments = assetnum_segments.get(assetnum)
        if not segments:
            log_fn(f"  pmnum {pmnum} assetnum {assetnum} has no geometry in ML source — skipping")
            continue
        # One asset can be split into several GIS segments; the HFML is all of
        # the segments joined end to end (see _asset_chain).
        chain, stray = _asset_chain(segments)
        if len(chain) > 1:
            log_fn(
                f"  MXASSETNUM {assetnum} is split into {len(chain)} connected segments "
                f"{_preview_oids([oid for oid, _ in chain])} — mapping them all as the HFML"
            )
        for oid, geom in stray:
            log_fn(
                f"  WARNING: duplicate MXASSETNUM {assetnum} — OID {oid} ({geom.length:.1f} ft) "
                "is a copy of, or doesn't connect to, the rest of the asset; ignored"
            )
        new_hfmls.append({"pmnum": pmnum, "geom": chain[0][1], "oids": [oid for oid, _ in chain]})

    if not new_hfmls:
        log_fn("No new HFMLs resolved to geometry — Phase 4 complete")
        _write_phase4_map_manifest([], log_fn)
        return

    log_fn(f"Mapping up to {MAX_HFMLS_PER_RUN} HFMLs this run")
    # One folder per run holds the PDFs plus a "Temp GIS Maps" subfolder with
    # each map saved as a project.  Created on the first export.
    run_dir = new_run_dir(config)
    log_fn(f"Run output folder: {run_dir}")

    # The endpoint index is only used to report gaps at the edge of a trace.
    # Built once per network: with the cache on, every HFML shares one network,
    # so this is built once per run rather than once per HFML.
    report_trace_gaps = bool(config.get("p4_report_trace_gaps", True))
    _endpoint_index_store = {}

    def _endpoint_index_for(geoms_obj):
        if not report_trace_gaps:
            return None
        token = id(geoms_obj)
        if token not in _endpoint_index_store:
            _endpoint_index_store.clear()
            _endpoint_index_store[token] = _endpoint_index(geoms_obj)
        return _endpoint_index_store[token]

    dest_paths = [hfml_layer, ml_dest, ll_dest, parcels_dest]
    _write_phase4_map_manifest([], log_fn)
    hfml_records = []
    failed = []
    for i, hfml in enumerate(new_hfmls, 1):
        if len(hfml_records) + len(failed) >= MAX_HFMLS_PER_RUN:
            log_fn(f"Reached {MAX_HFMLS_PER_RUN}-HFML limit — remaining {len(new_hfmls) - i + 1} left for the next run")
            break
        pmnum = hfml["pmnum"]
        start_geom = hfml["geom"]
        log_fn(f"--- HFML {i}/{len(new_hfmls)}: pmnum {pmnum} ---")
        timer.start_hfml()

        if cached_network is not None:
            directed_net, geoms, pipe_ups, pipe_dns, use_nodes = cached_network
        else:
            directed_net, geoms, pipe_ups, pipe_dns, use_nodes = _build_directed_network(
                arcpy, ml_source, start_geom, log_fn,
                snap_tolerance=float(config.get("p4_node_snap_tolerance_feet", 0.1) or 0.0),
            )

        # Use the HFML's own OIDs.  Matching it by endpoint coordinates could pick
        # another pipe between the same two manholes — and one digitized the
        # other way round would send the trace up from the downstream manhole.
        seed_oids = [oid for oid in hfml["oids"] if oid in geoms]
        seed_oid = hfml["oids"][0] if hfml["oids"][0] in geoms else None
        log_fn(
            f"Seed HFML OID in network: {seed_oid}"
            + (f" (+ {len(seed_oids) - 1} more segment(s) of the same asset)" if len(seed_oids) > 1 else "")
        )

        if seed_oid is None:
            reason = (
                "feature not in the cached network — the ML source may have changed "
                "since the cache was built"
                if cached_network is not None
                else "feature not in spatially filtered area"
            )
            log_fn(
                f"  ERROR: seed OID {hfml['oids'][0]} not found in network for pmnum {pmnum} — "
                f"{reason}. Skipping HFML (will not appear in manifest)."
            )
            continue

        # Snapshot destination OIDs so everything appended for this HFML can be
        # removed once its map is exported (or if anything fails midway).
        with timer.step("snapshot destinations"):
            snapshot = _snapshot_destinations(arcpy, dest_paths)
        try:
            with timer.step("append seed to HFML layer"):
                _append_seed_to_hfml_layer(arcpy, seed_oids, ml_source, config["p4_ml_layer_path"], pmnum, pm_mainlines_path, log_fn)

            with timer.step("upstream trace"):
                upstream_traced, downstream_key = _trace_upstream_directed(
                    seed_oids, directed_net, pipe_ups, pipe_dns, infer_direction=not use_nodes, log_fn=log_fn,
                    endpoint_index=_endpoint_index_for(geoms),
                )
            ml_oids = seed_oids + upstream_traced
            log_fn(f"Traced ML count (incl. seed): {len(ml_oids)}")
            log_fn(f"Traced ML OID preview: {_preview_oids(ml_oids)}")
            # Exclude the seed from the upstream ML trace layer — it belongs only
            # in the HFML layer (already written above via _append_seed_to_hfml_layer).
            upstream_ml_oids = list(upstream_traced)
            with timer.step("append upstream MLs"):
                _append_by_oids(arcpy, upstream_ml_oids, ml_source, ml_dest)
            log_fn(f"Appended {len(upstream_ml_oids)} upstream ML segments (seed excluded)")

            # LLs are found from the HFML and its upstream MLs only (ml_oids).
            # Selecting by the pipes that touch the HFML would also pull in the
            # downstream ML's laterals and parcels.  The trace says which end of
            # the HFML is downstream; with node fields its keys are node IDs, so
            # use the digitized end (lastPoint) of the segment ending there.
            if use_nodes:
                bottom = next((oid for oid in seed_oids if pipe_dns[oid] == downstream_key), seed_oid)
                downstream_point = (geoms[bottom][2], geoms[bottom][3])
            else:
                downstream_point = downstream_key
            with timer.step("select LLs (remote LL source)"):
                ll_oids = _select_lls_contacting_mls(
                    arcpy, ml_oids, ml_source, ll_source,
                    downstream_point=downstream_point, log_fn=log_fn,
                )
            log_fn(f"Selected LL count: {len(ll_oids)}")
            log_fn(f"Selected LL OID preview: {_preview_oids(ll_oids)}")
            with timer.step("append LLs"):
                _append_by_oids(arcpy, ll_oids, ll_source, ll_dest)
            log_fn(f"Appended {len(ll_oids)} LLs")

            with timer.step("select parcels (remote parcels source)"):
                parcel_oids = _select_parcels_intersecting_lls(
                    arcpy, ll_oids, ll_source, parcels_source,
                    ml_oid_list=ml_oids, ml_source=ml_source, log_fn=log_fn,
                )
            log_fn(f"Selected parcel count: {len(parcel_oids)}")
            log_fn(f"Selected parcel OID preview: {_preview_oids(parcel_oids)}")
            with timer.step("append parcels"):
                _append_by_oids(arcpy, parcel_oids, parcels_source, parcels_dest)
            log_fn(f"Appended {len(parcel_oids)} parcels")

            record = {
                "pmnum": pmnum,
                "seed_oid": seed_oid,
                "seed_oids": list(seed_oids),
                # ml_oids = upstream traced pipes only (seed is in the HFML layer,
                # not in ml_dest, so it is tracked separately via seed_oid).
                "ml_oids": list(upstream_ml_oids),
                "ll_oids": list(ll_oids),
                "parcel_oids": list(parcel_oids),
                "counts": {"ml": len(upstream_ml_oids), "ll": len(ll_oids), "parcels": len(parcel_oids)},
            }

            log_fn(f"Exporting map for HFML {pmnum}...")
            record["pdf_path"], record["draft_aprx_path"] = run_output_paths(run_dir, pmnum)
            record["aprx_path"] = aprx_path
            # The map export runs in a separate process that loads config.json
            # itself.  The object IDs in this record belong to whichever copy of
            # each source Phase 4 actually read, so tell it which ones those
            # were -- otherwise a local copy's IDs would be looked up in the
            # remote source and the map would be drawn from the wrong features.
            if source_overrides:
                record["source_overrides"] = dict(source_overrides)
            with timer.step("map export (separate process)"):
                export_hfml_map_out_of_process(record, config, log_fn)
        except Exception as exc:
            log_fn(f"  ERROR: HFML {pmnum} failed — {exc}")
            failed.append(pmnum)
            continue
        finally:
            with timer.step("cleanup appended rows"):
                _cleanup_destinations(arcpy, snapshot, log_fn)
            timer.log_hfml(pmnum)

        hfml_records.append(record)
        # Write manifest after every HFML so Phase 5 can verify at any point,
        # even if Phase 4 is stopped midway.
        _write_phase4_map_manifest(hfml_records, log_fn)
        log_fn(f"HFML {pmnum} complete — map saved, appended features removed")

    timer.log_run()

    if failed:
        raise RuntimeError(
            f"{len(failed)} HFML(s) failed and were rolled back: {failed} "
            f"({len(hfml_records)} mapped successfully)"
        )
    else:
        log_fn(f"Phase 4 complete — {len(hfml_records)} HFMLs mapped")

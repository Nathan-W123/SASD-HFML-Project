import math
import os
import platform
import re
import sys
import time
from contextlib import contextmanager
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import json

from backend.phase4.network_cache import (
    Phase4Cache, _gdb_root, _table_files, dataset_signature, default_cache_dir, free_gb,
    signature_is_weak,
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
# Raised from 5 to 25 on 2026-11-19, once a map after the first took ~12s.
MAX_HFMLS_PER_RUN = 25
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


# Waits for reads of PM_Mainlines, which the share has twice briefly reported
# missing after Phase 3 rewrote it -- once three minutes later (2026-11-19).
# Matches the export's schedule (_PM_READ_WAITS in phase5).
PM_READ_WAITS = (2, 5, 10)


def _with_arcpy_retry(arcpy, action, context, waits=None):
    """Run action, retrying after clearing the workspace cache.

    waits are the pauses between attempts; by default 1s then 2s, i.e. three
    attempts, as before.
    """
    if waits is None:
        waits = tuple(range(1, ARCPY_RETRY_ATTEMPTS))
    attempts = len(waits) + 1
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except Exception as exc:
            last_exc = exc
            if attempt == attempts:
                break
            _clear_workspace_cache(arcpy)
            time.sleep(waits[attempt - 1])
    raise RuntimeError(
        f"{context} failed after {attempts} attempts. Last error: {last_exc}"
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


@contextmanager
def _quiet_geoprocessing(arcpy, log_fn=None):
    """Switch off ArcGIS Pro's per-tool bookkeeping for the duration.

    Measured on 2026-11-17: every geoprocessing tool call in Phase 4 cost 3-6s
    whatever it did -- CreateFeatureclass + two AddFields ~16s, each Delete ~3s,
    while the row inserts and Describes they surround cost 0.0s.  That floor is
    Pro writing geoprocessing history and lineage metadata on every call, and
    with the project on a network share each write pays a round trip.

    Turned off: history logging, metadata logging, and adding tool outputs to
    the open map.  The previous values are restored afterwards, including on
    failure, so the user's Pro session is left as it was found.  Each setting is
    skipped if this Pro version lacks it.
    """
    saved = []

    def _switch(label, getter, setter):
        try:
            previous = getter()
        except Exception:
            return
        try:
            setter(False)
        except Exception:
            return
        saved.append((label, setter, previous))

    for label, get_name, set_name in (
        ("history logging", "GetLogHistory", "SetLogHistory"),
        ("metadata logging", "GetLogMetadata", "SetLogMetadata"),
    ):
        getter = getattr(arcpy, get_name, None)
        setter = getattr(arcpy, set_name, None)
        if getter is not None and setter is not None:
            _switch(label, getter, setter)
    env = getattr(arcpy, "env", None)
    if env is not None and hasattr(env, "addOutputsToMap"):
        _switch(
            "adding outputs to the map",
            lambda: env.addOutputsToMap,
            lambda v: setattr(env, "addOutputsToMap", v),
        )

    if log_fn:
        if saved:
            log_fn(
                "Geoprocessing bookkeeping off for this phase: "
                + ", ".join(label for label, _, _ in saved)
                + " (restored afterwards)"
            )
        else:
            log_fn("Geoprocessing bookkeeping: no switchable settings in this Pro version")
    try:
        yield
    finally:
        for label, setter, previous in reversed(saved):
            try:
                setter(previous)
            except Exception as exc:
                if log_fn:
                    log_fn(f"  WARNING: could not restore {label} — {exc}")


_SCRATCH_WORKSPACE = {}


def _scratch_workspace(arcpy, log_fn=None):
    """Which in-memory workspace scratch feature classes go in.

    Only the per-HFML fallback network build uses scratch feature classes now;
    the lateral and parcel tests that used to build them twice per HFML run in
    Python instead (see _ends_near_lines).

    Probed once per process, falling back to 'in_memory' so an environment
    without it still works.
    """
    if "name" not in _SCRATCH_WORKSPACE:
        name = "in_memory"
        try:
            probe = "memory\\_hfml_probe"
            if arcpy.Exists(probe):
                arcpy.management.Delete(probe)
            arcpy.management.CreateFeatureclass("memory", "_hfml_probe", "POINT")
            arcpy.management.Delete(probe)
            name = "memory"
        except Exception as exc:
            if log_fn:
                log_fn(f"  Scratch workspace 'memory' unavailable ({exc}) — using 'in_memory'")
        _SCRATCH_WORKSPACE["name"] = name
        if log_fn:
            log_fn(f"  Scratch workspace: {name}")
    return _SCRATCH_WORKSPACE["name"]


def _geom_to_temp_fc(arcpy, geom, name):
    workspace = _scratch_workspace(arcpy)
    path = f"{workspace}\\{name}"
    if arcpy.Exists(path):
        arcpy.management.Delete(path)
    arcpy.management.CreateFeatureclass(workspace, name, "POLYLINE", spatial_reference=geom.spatialReference)
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


# Seconds to wait before asking again whether a destination is free.  A map
# export process from a run that has just ended can take a few seconds to close.
WRITE_LOCK_WAITS = (2, 5)

# A file geodatabase lock file: <table>.<computer>.<process id>.<thread>.<kind>.lock
_LOCK_FILE = re.compile(r"^(?P<table>[^.]+)\.(?P<host>[^.]+)\.(?P<pid>\d+)\.\d+\.(?P<kind>[a-z]+)\.lock$", re.I)
_LOCK_KINDS = {"sr": "reading", "ed": "editing", "wr": "writing", "rd": "reading"}


def _lock_holders(arcpy, path):
    """Which computers and processes hold locks on this table, read from the
    geodatabase's lock files: [(computer, process id, [what])].  Locks on the
    table itself when its files can be identified, else on the geodatabase.
    Empty when there are none or they cannot be read."""
    root = _gdb_root(path)
    if not root or not os.path.isdir(root):
        return []
    try:
        names = os.listdir(root)
    except OSError:
        return []
    table_id = None
    try:
        found = _table_files(arcpy, path, int(arcpy.management.GetCount(path)[0]))
        table_id = found[0] if found else None
    except Exception:
        pass
    holders = {}
    for name in names:
        m = _LOCK_FILE.match(name)
        if not m:
            continue
        if table_id and m.group("table").lower() != table_id:
            continue
        key = (m.group("host"), int(m.group("pid")))
        holders.setdefault(key, set()).add(_LOCK_KINDS.get(m.group("kind").lower(), m.group("kind")))
    return [(host, pid, sorted(kinds)) for (host, pid), kinds in sorted(holders.items())]


def _describe_lock_holders(holders):
    me_host = platform.node().split(".")[0].lower()
    me_pid = os.getpid()
    lines = []
    for host, pid, kinds in holders:
        what = " and ".join(kinds)
        if host.lower() == me_host and pid == me_pid:
            continue    # this ArcGIS Pro, which runs the tool
        if host.lower() == me_host:
            lines.append(
                f"  - process {pid} on this computer ({what}). In Task Manager > Details, find PID {pid}: "
                "python.exe is a map export left from an earlier run and can be ended; "
                "ArcGISPro.exe is another ArcGIS Pro window with this data open."
            )
        else:
            lines.append(f"  - computer {host}, process {pid} ({what}): someone there has this data open.")
    return lines


def _require_write_lock(arcpy, path, label, log_fn=None, waits=WRITE_LOCK_WAITS):
    for wait in (0,) + tuple(waits):
        if wait:
            if log_fn:
                log_fn(f"  {label} is in use — checking again in {wait}s")
            time.sleep(wait)
        if arcpy.TestSchemaLock(path):
            return
    holders = _describe_lock_holders(_lock_holders(arcpy, path))
    raise RuntimeError(
        f"{label} is locked and cannot be written: {path}. "
        + ("Held by:\n" + "\n".join(holders) + "\n" if holders else "")
        + "Close ArcGIS Pro maps, attribute tables, edit sessions, Catalog previews, "
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
        _require_write_lock(arcpy, path, label, log_fn)
    log_fn("Phase 4 destination write locks available")
    _scratch_workspace(arcpy, log_fn)
    _report_spatial_references(arcpy, config, log_fn)
    # A select-by-location scans the whole layer however few features it picks,
    # so these counts set the floor on what each per-HFML select can cost.
    for label, key in (
        ("ML", "p4_ml_source_path"),
        ("LL", "p4_ll_source_path"),
        ("parcels", "p4_parcels_source_path"),
    ):
        try:
            log_fn(f"  {label} source feature count: {int(arcpy.management.GetCount(config[key])[0])}")
        except Exception as exc:
            log_fn(f"  {label} source feature count: unavailable ({exc})")

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
        self.sub_totals = {}
        self.current = {}
        self.current_sub = {}

    @contextmanager
    def _record(self, label, totals, current):
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            current[label] = current.get(label, 0.0) + elapsed
            totals[label] = totals.get(label, 0.0) + elapsed

    def step(self, label):
        return self._record(label, self.run_totals, self.current)

    def substep(self, label):
        """Time a step inside another step.

        Kept in a separate tally: a sub-step's time is already counted by the
        step that contains it, so mixing them would double count and make the
        percentages meaningless.
        """
        return self._record(label, self.sub_totals, self.current_sub)

    def start_hfml(self):
        self.current = {}
        self.current_sub = {}

    @staticmethod
    def _parts(totals):
        return ", ".join(
            f"{label} {seconds:.1f}s"
            for label, seconds in sorted(totals.items(), key=lambda kv: -kv[1])
        )

    def log_hfml(self, pmnum):
        if self.current:
            total = sum(self.current.values())
            self.log_fn(f"  Timing for {pmnum} — {total:.1f}s total: {self._parts(self.current)}")
        if self.current_sub:
            self.log_fn(f"  Inside those steps: {self._parts(self.current_sub)}")

    def log_run(self):
        if self.run_totals:
            total = sum(self.run_totals.values())
            self.log_fn(f"Phase 4 time by step ({total:.1f}s across all HFMLs):")
            for label, seconds in sorted(self.run_totals.items(), key=lambda kv: -kv[1]):
                share = (seconds / total * 100) if total else 0
                self.log_fn(f"    {label}: {seconds:.1f}s ({share:.0f}%)")
        if self.sub_totals:
            self.log_fn("Inside those steps (already counted above):")
            for label, seconds in sorted(self.sub_totals.items(), key=lambda kv: -kv[1]):
                self.log_fn(f"    {label}: {seconds:.1f}s")


class _NullTimer:
    """Stands in when no timer is passed, so the timed code reads the same."""

    @contextmanager
    def substep(self, label):
        yield


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


def _against_flow_run(start_oid, outflows, pipe_dns, visited, stop_keys, blocked_keys, allowed=None):
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
            if allowed is not None and oid not in allowed:
                continue  # a main outside the status filter is not part of the network
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
    endpoint_index=None, allowed=None,
):
    """DFS upstream from the HFML (seed_oids: its segments, longest first).
    Returns (upstream OIDs, the HFML's downstream key).

    Pipes flowing into a traced manhole are followed.  With infer_direction
    (keys are coordinates, so direction comes from digitizing), pipes digitized
    against the flow are followed too (see _against_flow_run), and an HFML
    digitized against the flow is traced from its other end.

    Every map is cleaned up after export, so pipes already present in the
    destination (e.g. from older runs) are traced through like any other.

    allowed, when given, is the set of mains the trace may use (the main status
    filter).  A main outside it is treated as absent: not mapped, not followed,
    so nothing reached only through it is mapped either.  The HFML's own
    segments are always mapped, whatever their status.
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
    to_append, against_flow, looped, stopped = [], [], [], []
    stack, queued = list(start_keys), set(start_keys)
    while stack:
        key = stack.pop()
        for oid, from_key in directed_net.get(key, []):
            if oid in visited:
                continue
            if allowed is not None and oid not in allowed:
                visited.add(oid)
                stopped.append(oid)
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
            if allowed is not None and oid not in allowed:
                visited.add(oid)
                continue
            for run_oid in _against_flow_run(
                oid, outflows, pipe_dns, visited, queued, blocked, allowed=allowed,
            ) or []:
                visited.add(run_oid)
                to_append.append(run_oid)
                against_flow.append(run_oid)
                if pipe_dns[run_oid] not in queued:
                    queued.add(pipe_dns[run_oid])
                    stack.append(pipe_dns[run_oid])

    log(f"Upstream trace found {len(to_append)} pipes from the HFML's upstream end {up_key!r}")
    if stopped:
        log(
            f"  Stopped at {len(stopped)} main(s) outside the main status filter — not mapped, "
            f"and nothing upstream of them is: {_preview_oids(sorted(stopped))}"
        )
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


# Field plans for the direct row copy, per (source, destination), for this run.
_APPEND_PLANS = {}
_SKIP_FIELD_TYPES = {"OID", "Geometry", "GlobalID", "Blob", "Raster"}


def _append_plan(arcpy, source, destination):
    """How to copy rows from source to destination without the Append tool.

    Mirrors Append NO_TEST: fields are matched by name, ignoring case; a
    destination field with no match is left empty; object IDs, geometry
    bookkeeping fields and GlobalIDs are not copied.  The direct copy is only
    used when both sides share a coordinate system, since a cursor does not
    reproject the way Append does.
    """
    key = (source, destination)
    plan = _APPEND_PLANS.get(key)
    if plan is not None:
        return plan
    source_fields = {
        f.name.lower(): f for f in arcpy.ListFields(source) if f.type not in _SKIP_FIELD_TYPES
    }
    pairs = []
    for f in arcpy.ListFields(destination):
        if f.type in _SKIP_FIELD_TYPES or not f.editable:
            continue
        match = source_fields.get(f.name.lower())
        if match is not None:
            pairs.append((match.name, f.name, match.type, f.type))
    try:
        src_sr = arcpy.Describe(source).spatialReference
        dst_sr = arcpy.Describe(destination).spatialReference
        same_sr = (src_sr.factoryCode, src_sr.name) == (dst_sr.factoryCode, dst_sr.name)
    except Exception:
        same_sr = False
    plan = {"pairs": pairs, "direct": same_sr, "reason": None if same_sr else "coordinate systems differ"}
    _APPEND_PLANS[key] = plan
    return plan


# A direct copy that fails is tried again on the next call; only this many
# failures in a row switch that source/destination pair to the Append tool for
# the rest of the run.  2026-11-19: one brief lock on PMs_SSGRAVITYMAIN at the
# first HFML used to cost the Append tool's ~4s on every later HFML.
DIRECT_COPY_MAX_FAILURES = 2
# Seconds to wait before trying again after a lock.  The Append run straight
# after that 2026-11-19 lock went through, so locks clear quickly.
DIRECT_COPY_LOCK_WAIT = 2.0


class _DirectCopyLeftRows(RuntimeError):
    """A direct copy failed part-way and some of its rows could not be removed.

    `copied` holds the SOURCE object IDs whose rows are in the destination, so
    the caller appends only the rest and no row ends up there twice.
    """

    def __init__(self, message, copied):
        super().__init__(message)
        self.copied = set(copied)


class _DirectCopyUnknown(RuntimeError):
    """A direct copy failed part-way and which rows it left behind is unknown.

    Not caught by the fallback: appending again could duplicate rows, so the
    HFML fails instead and is rolled back and picked up again next run.
    """


def _is_lock_error(exc):
    return "lock" in str(exc).lower()


def _delete_oids(arcpy, path, oids):
    for chunk in _chunks(sorted(set(oids))):
        with arcpy.da.UpdateCursor(path, ["OID@"], _oid_where(chunk)) as cursor:
            for _ in cursor:
                cursor.deleteRow()


def _existing_oids(arcpy, path, oids):
    found = set()
    for chunk in _chunks(sorted(set(oids))):
        with arcpy.da.SearchCursor(path, ["OID@"], _oid_where(chunk)) as cursor:
            found.update(row[0] for row in cursor)
    return found


def _undo_partial_copy(arcpy, destination, inserted, error):
    """Remove the rows a failed direct copy inserted.

    Returns quietly when none of them are left in the destination -- removed
    here, or never committed, as when the insert cursor itself hit the lock.
    Raises _DirectCopyLeftRows naming the rows that are there and could not be
    removed, or _DirectCopyUnknown when even that cannot be read.
    """
    dst_oids = [dst for _, dst in inserted if dst is not None]
    if not dst_oids:
        return
    cleanup_error = None
    for wait in (0, DIRECT_COPY_LOCK_WAIT):
        if wait:
            time.sleep(wait)
        try:
            _delete_oids(arcpy, destination, dst_oids)
            return
        except Exception as exc:
            cleanup_error = exc
    try:
        present = _existing_oids(arcpy, destination, dst_oids)
    except Exception as check_error:
        raise _DirectCopyUnknown(
            f"direct copy into {destination} failed ({error}); its {len(dst_oids)} partial "
            f"row(s) could not be removed ({cleanup_error}) or checked ({check_error})"
        ) from error
    if present:
        raise _DirectCopyLeftRows(
            f"direct copy into {destination} failed ({error}) and {len(present)} of its rows "
            f"could not be removed ({cleanup_error})",
            {src for src, dst in inserted if dst in present},
        ) from error


def _append_rows_directly(arcpy, oid_list, source, destination, plan):
    """Copy rows with a search cursor into an insert cursor.

    The Append tool measured 4-9s per call (2026-11-17), the same fixed cost
    every other geoprocessing tool showed, for a handful of rows.  If anything
    fails part-way the rows inserted so far are removed again (see
    _undo_partial_copy), so the caller can fall back without leaving duplicates.
    """
    pairs = plan["pairs"]
    src_names = ["OID@", "SHAPE@"] + [p[0] for p in pairs]
    dst_names = ["SHAPE@"] + [p[1] for p in pairs]
    inserted = []  # (source OID, destination OID)
    try:
        with arcpy.da.InsertCursor(destination, dst_names) as insert:
            for chunk in _chunks(sorted(set(oid_list))):
                with arcpy.da.SearchCursor(source, src_names, _oid_where(chunk)) as cursor:
                    for row in cursor:
                        values = [row[1]]
                        for value, (_, _, src_type, dst_type) in zip(row[2:], pairs):
                            values.append(value if src_type == dst_type else _coerce_for_field(value, dst_type))
                        inserted.append((row[0], insert.insertRow(values)))
    except Exception as exc:
        _undo_partial_copy(arcpy, destination, inserted, exc)
        raise
    return len(inserted)


def _try_direct_copy(arcpy, oid_list, source, destination, plan, log_fn=None):
    """Copy with the direct row copy.  Returns the source OIDs still to append.

    A lock gets one more try after a short wait.  Whatever is left after that
    goes to the Append tool, and the direct copy is tried again on the next
    call unless it has now failed DIRECT_COPY_MAX_FAILURES times in a row.
    """
    remaining = list(oid_list)
    error = None
    for attempt in (1, 2):
        try:
            _append_rows_directly(arcpy, remaining, source, destination, plan)
            plan["failures"] = 0
            return []
        except _DirectCopyUnknown:
            raise
        except _DirectCopyLeftRows as exc:
            remaining = [oid for oid in remaining if oid not in exc.copied]
            error = exc
        except Exception as exc:
            error = exc
        if not remaining:
            plan["failures"] = 0
            return []
        if attempt == 1 and _is_lock_error(error):
            if log_fn:
                log_fn(
                    f"  NOTE: {destination} was locked ({error}) — "
                    f"trying the direct row copy again in {DIRECT_COPY_LOCK_WAIT:g}s"
                )
            time.sleep(DIRECT_COPY_LOCK_WAIT)
            continue
        break

    plan["failures"] = plan.get("failures", 0) + 1
    if plan["failures"] >= DIRECT_COPY_MAX_FAILURES:
        plan["direct"] = False
        plan["reason"] = str(error)
        then = "using the Append tool for it for the rest of this run"
    else:
        then = "using the Append tool for these rows; the direct copy is tried again next time"
    if log_fn:
        log_fn(f"  NOTE: direct row copy into {destination} failed ({error}) — {then}")
    return remaining


def _append_by_oids(arcpy, oid_list, source, destination, log_fn=None):
    if not oid_list:
        return
    plan = _append_plan(arcpy, source, destination)
    if plan["direct"]:
        oid_list = _try_direct_copy(arcpy, oid_list, source, destination, plan, log_fn)
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


# The endpoint tests below used to build scratch point feature classes, run a
# select-by-location over them, then delete everything again -- for every HFML.
# Timed per tool call (2026-11-17) those schema operations cost 3-6s each
# however little data they held: ~75s of every HFML.  Selections and cursor
# reads cost ~0.  So the selections stay, the scratch feature classes go, and
# the one-foot endpoint test runs on geometry objects in Python instead.

LL_SEARCH_FEET = 1.0
# Spatial selections are evaluated with the data's XY tolerance (~0.001 ft for
# these State Plane feet layers), so a point a hair past the search distance
# can still count.  Matching that keeps the Python test from being stricter
# than the select it replaces.
_DISTANCE_TOLERANCE = 0.001
# Rows per OBJECTID IN (...) clause.  Long lists are split rather than sent as
# one query that a geodatabase can refuse.
_OID_CHUNK = 900


def _chunks(values, size=_OID_CHUNK):
    values = list(values)
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _oid_where(oids):
    return f"OBJECTID IN ({','.join(str(int(oid)) for oid in oids)})"


def _read_geometries(arcpy, source, oids, extra_where=None):
    """{oid: geometry} for these OIDs, straight from the dataset.

    Reads the dataset by path, never through a layer, so an empty selection
    can't silently widen the read to every feature.  extra_where narrows the
    read further (e.g. the lateral status filter).
    """
    out = {}
    for chunk in _chunks(sorted(set(oids))):
        where = _oid_where(chunk)
        if extra_where:
            where = f"({where}) AND ({extra_where})"
        with arcpy.da.SearchCursor(source, ["OID@", "SHAPE@"], where) as cursor:
            for oid, geom in cursor:
                if geom is not None:
                    out[oid] = geom
    return out


def _selected_oids(arcpy, layer):
    """OIDs currently selected on a layer.

    Read from the selection set itself.  GetCount and cursors treat an empty
    selection as "no selection" and return every feature, which on a 300,000-
    feature layer would be both wrong and very slow.
    """
    fid_set = arcpy.Describe(layer).FIDSet or ""
    return {int(part) for part in fid_set.replace(",", ";").split(";") if part.strip()}


def _select_by_oids(arcpy, layer, oids):
    """Select exactly these OIDs on a layer, in chunks.  Never call with none:
    an empty selection means "all features" to every tool that reads it."""
    oids = sorted(set(oids))
    if not oids:
        raise ValueError("_select_by_oids needs at least one OID")
    for i, chunk in enumerate(_chunks(oids)):
        arcpy.management.SelectLayerByAttribute(
            layer, "NEW_SELECTION" if i == 0 else "ADD_TO_SELECTION", _oid_where(chunk)
        )


def _endpoints(geom):
    """(x, y) of a line's two ends, as the old endpoint table recorded them."""
    out = []
    for point in (geom.firstPoint, geom.lastPoint):
        if point is not None:
            out.append((point.X, point.Y))
    return out


def _ends_near_lines(candidates, lines, distance, make_point):
    """For each candidate line, the ends that lie within `distance` of any of `lines`.

    candidates: {oid: geometry}; lines: [geometry].  Returns {oid: [(x, y), ...]}
    for candidates with at least one such end.  This is what building an
    endpoint feature class and selecting it WITHIN_A_DISTANCE of the lines
    computed, without any geoprocessing tools.
    """
    limit = distance + _DISTANCE_TOLERANCE
    boxes = [(line, line.extent) for line in lines]
    hits = {}
    for oid, geom in candidates.items():
        for x, y in _endpoints(geom):
            point = None
            for line, ext in boxes:
                if not (ext.XMin - limit <= x <= ext.XMax + limit
                        and ext.YMin - limit <= y <= ext.YMax + limit):
                    continue
                if point is None:
                    point = make_point(x, y)
                if line.distanceTo(point) <= limit:
                    hits.setdefault(oid, []).append((x, y))
                    break
    return hits


def _point_maker(arcpy, like_geom):
    sr = getattr(like_geom, "spatialReference", None)
    return lambda x, y: arcpy.PointGeometry(arcpy.Point(x, y), sr)


class _LayerPool:
    """Feature layers made once per run and reused for every HFML.

    Making a layer is cheap, but each Delete measured ~3s, and the old code
    made and deleted five to seven layers per HFML.  Every use replaces the
    layer's selection, so nothing carries over from one HFML to the next.
    """

    def __init__(self, arcpy):
        self.arcpy = arcpy
        self._layers = {}

    def layer(self, key, source):
        name = f"_HFMLPool_{key}"
        entry = self._layers.get(key)
        if entry is not None and entry == source:
            return name
        _replace_temp_layer(self.arcpy, name, source)
        self._layers[key] = source
        return name

    def close(self):
        for key in list(self._layers):
            name = f"_HFMLPool_{key}"
            try:
                if self.arcpy.Exists(name):
                    self.arcpy.management.Delete(name)
            except Exception:
                pass
        self._layers.clear()


@contextmanager
def _layer_pool(arcpy):
    pool = _LayerPool(arcpy)
    try:
        yield pool
    finally:
        pool.close()


def _clear_leftover_trace_rows(arcpy, paths, log_fn):
    """Delete rows already in the trace destination tables before mapping.

    Each map's rows are removed after it is exported, so at the start of a run
    these tables should be empty.  Anything there was left by a run that did
    not finish cleaning up, and it is drawn on every later map that covers its
    area -- HFML 10774 drew four such mains and thirteen laterals on
    2026-11-17.  The HFML layer (PMs_SSGRAVITYMAIN) is deliberately not passed
    in: a seed left there marks its HFML as already mapped.

    Assumes one pipeline run at a time, as the per-map cleanup already does.
    """
    for path in paths:
        def _delete(path=path):
            removed = []
            with arcpy.da.UpdateCursor(path, ["OID@"]) as cursor:
                for (oid,) in cursor:
                    cursor.deleteRow()
                    removed.append(oid)
            return removed

        removed = _with_arcpy_retry(arcpy, _delete, f"Failed to remove leftover rows from {path}")
        if removed:
            log_fn(
                f"Removed {len(removed)} leftover row(s) from an earlier run in {path}: "
                f"{_preview_oids(sorted(removed))}"
            )


def _check_status_filter(arcpy, source, status_filter, log_fn, label="lateral",
                         config_key="p4_ll_status_filter"):
    """Return a status filter if it is usable, else None.

    Tried once against the source before any HFML.  A filter that is invalid --
    a renamed field, a typo -- or that matches nothing at all would silently
    empty every map, so either case switches it off and says so.
    """
    status_filter = (status_filter or "").strip()
    title = label[:1].upper() + label[1:]
    if not status_filter:
        log_fn(f"{title} status filter: off ({config_key} is empty) — all statuses used")
        return None
    try:
        with arcpy.da.SearchCursor(source, ["OID@"], status_filter) as cursor:
            matched = next(iter(cursor), None) is not None
    except Exception as exc:
        log_fn(
            f"  WARNING: {label} status filter {status_filter!r} is not valid for the {label} "
            f"source ({exc}) — using {label}s of every status this run"
        )
        return None
    if not matched:
        log_fn(
            f"  WARNING: {label} status filter {status_filter!r} matches no {label}s at all — "
            f"using {label}s of every status this run"
        )
        return None
    log_fn(f"{title} status filter: {status_filter}")
    return status_filter


def _select_lls_contacting_mls(
    arcpy, ml_oid_list, ml_source, ll_source, downstream_point=None, log_fn=None,
    timer=None, layers=None, status_filter=None,
):
    """Laterals with an end within LL_SEARCH_FEET of a traced main.

    One select-by-location narrows 300,000+ laterals to the few near the trace;
    the endpoint test then runs on their geometry in Python.
    """
    if not ml_oid_list:
        return []
    timer = timer or _NullTimer()
    own_pool = layers is None
    pool = layers or _LayerPool(arcpy)
    try:
        with timer.substep("LL: select laterals near the trace"):
            ml_layer = pool.layer("ml", ml_source)
            _select_by_oids(arcpy, ml_layer, ml_oid_list)
            if not _selected_oids(arcpy, ml_layer):
                return []
            ll_layer = pool.layer("ll", ll_source)
            arcpy.management.SelectLayerByLocation(
                ll_layer,
                "WITHIN_A_DISTANCE",
                ml_layer,
                search_distance=LL_SEARCH_DISTANCE,
                selection_type="NEW_SELECTION",
            )
            candidate_oids = _selected_oids(arcpy, ll_layer)

        if log_fn:
            log_fn(f"Candidate LL count before endpoint filter: {len(candidate_oids)}")
        if not candidate_oids:
            if log_fn:
                log_fn(f"LL endpoint hits after {LL_SEARCH_DISTANCE} filter: 0")
            return []

        with timer.substep("LL: read laterals and traced MLs"):
            # Laterals outside the status filter are dropped here, before the
            # endpoint test, so they are never appended and their parcels are
            # never selected -- the same laterals the map template hides.
            candidates = _read_geometries(arcpy, ll_source, candidate_oids, extra_where=status_filter)
            mains = list(_read_geometries(arcpy, ml_source, ml_oid_list).values())
        if status_filter and log_fn:
            skipped = len(candidate_oids) - len(candidates)
            if skipped:
                log_fn(f"Skipped {skipped} lateral(s) outside the status filter ({status_filter})")

        with timer.substep("LL: endpoint test (Python)"):
            hits = (
                _ends_near_lines(candidates, mains, LL_SEARCH_FEET, _point_maker(arcpy, mains[0]))
                if mains else {}
            )

        if log_fn:
            log_fn(
                f"LL endpoint hits after {LL_SEARCH_DISTANCE} filter: "
                f"{sum(len(points) for points in hits.values())}"
            )

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
        if own_pool:
            pool.close()


def _select_parcels_intersecting_lls(
    arcpy,
    ll_oid_list,
    ll_source,
    parcels_source,
    ml_oid_list=None,
    ml_source=None,
    log_fn=None,
    timer=None,
    layers=None,
):
    """The parcel each selected lateral serves.

    Each lateral's parcel-side end -- the end away from the main -- takes the
    parcel(s) it lies in or on (a shared edge gives both); an end in no parcel
    takes the nearest one within PARCEL_SEARCH_FEET.
    """
    if not ll_oid_list:
        return []
    timer = timer or _NullTimer()
    own_pool = layers is None
    pool = layers or _LayerPool(arcpy)
    try:
        with timer.substep("parcels: read laterals and traced MLs"):
            laterals = _read_geometries(arcpy, ll_source, ll_oid_list)
            mains = (
                list(_read_geometries(arcpy, ml_source, ml_oid_list).values())
                if ml_oid_list and ml_source else []
            )

        # Per-LL classification: for each LL that has at least one ML-side
        # end, its parcel-side end is the OTHER one.  Classifying per LL rather
        # than by global set subtraction keeps a parcel-side end that happens
        # to also lie on an ML boundary.
        with timer.substep("parcels: classify LL ends (Python)"):
            ml_side = _ends_near_lines(
                laterals, mains, LL_SEARCH_FEET, _point_maker(arcpy, mains[0])
            ) if mains else {}
            parcel_points = []
            for ll_oid in sorted(laterals):
                ends = _endpoints(laterals[ll_oid])
                near = ml_side.get(ll_oid)
                if not near:
                    continue  # LL has no ML connection — skip
                parcel_side = [end for end in ends if end not in near]
                # Both ends within ML search distance (very short LL): include
                # both and let the parcel matching decide.
                parcel_points.extend(parcel_side if parcel_side else ends)

        if log_fn:
            log_fn(f"LL parcel-side endpoint count: {len(parcel_points)}")
        if not parcel_points:
            return []

        # Candidates: parcels within PARCEL_SEARCH_FEET of the selected laterals
        # anywhere along their length.  Every parcel near a parcel-side end is
        # near its lateral, so this is a superset of what the old point-based
        # select returned, and the exact per-end test below picks the same
        # parcels from it -- without building a point feature class to select by.
        with timer.substep("parcels: select parcels near the laterals"):
            ll_layer = pool.layer("ll", ll_source)
            _select_by_oids(arcpy, ll_layer, ll_oid_list)
            parcels_layer = pool.layer("parcels", parcels_source)
            arcpy.management.SelectLayerByLocation(
                parcels_layer, "WITHIN_A_DISTANCE", ll_layer,
                search_distance=f"{PARCEL_SEARCH_FEET} Feet", selection_type="NEW_SELECTION",
            )
            candidate_oids = _selected_oids(arcpy, parcels_layer)

        with timer.substep("parcels: read candidate parcels"):
            candidates = [
                (oid, geom, geom.extent)
                for oid, geom in sorted(_read_geometries(arcpy, parcels_source, candidate_oids).items())
            ]

        chosen, nearest_used, unmatched = set(), [], 0
        tol = PARCEL_SEARCH_FEET
        with timer.substep("parcels: match ends to parcels"):
            make_point = _point_maker(arcpy, candidates[0][1] if candidates else (mains[0] if mains else None))
            for x, y in parcel_points:
                end = make_point(x, y)
                dists = sorted(
                    (geom.distanceTo(end), oid) for oid, geom, ext in candidates
                    if ext.XMin - tol <= x <= ext.XMax + tol and ext.YMin - tol <= y <= ext.YMax + tol
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
        if own_pool:
            pool.close()


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

    _append_by_oids(arcpy, list(seed_oids), ml_source, hfml_layer, log_fn=log_fn)

    if not has_pmnum_field:
        log_fn(f"  HFML seed appended for pmnum {pmnum} ('{pmnum_field}' not found — attributes not stamped)")
        return

    # Stamp pmnum and copy the PM_Mainlines attributes (frequency etc.) onto the
    # seed so the layer's symbology classifies it.  HFML layer fields are matched
    # as PM_Mainlines_<field> (joined export naming) or a bare 'frequency'.
    layer_field_info = {f.name.lower(): f for f in arcpy.ListFields(hfml_layer)}

    # Phase 3 has just rewritten PM_Mainlines on the share, and the first read
    # straight after can see a stale workspace ("PM_Mainlines does not exist",
    # 2026-11-17).  _with_arcpy_retry clears the workspace cache and retries.
    def _read_pm_values():
        pm_fields = [
            f.name for f in arcpy.ListFields(pm_mainlines_path)
            if f.type not in ("OID", "Geometry", "GlobalID", "Blob", "Raster")
        ]
        with arcpy.da.SearchCursor(pm_mainlines_path, pm_fields, f"pmnum = '{pmnum}'") as cur:
            for row in cur:
                return dict(zip(pm_fields, row))
        return {}

    # If it still cannot be read, fail this HFML rather than map it without its
    # attributes: a map that exports is marked done and never redone, while a
    # failed HFML is rolled back and picked up again on the next run.
    pm_values = _with_arcpy_retry(
        arcpy, _read_pm_values, f"Reading PM_Mainlines attributes for pmnum {pmnum}",
        waits=PM_READ_WAITS,
    )

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


def _log_fingerprint_notes(label, signature, notes, log_fn):
    """Say why a source is fingerprinted by its whole geodatabase, if it is.

    That scope means any other layer in the same geodatabase changing forces
    this source to be copied again, so the reason is worth seeing in the log.
    """
    if signature.get("scope") == "geodatabase" and notes:
        log_fn(
            f"  {label} source: watching the whole geodatabase, since its own table "
            f"files were not found — {'; '.join(notes)}"
        )


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

    notes = []
    signature = dataset_signature(
        arcpy, ml_source,
        previous=cache.stored_signature("ml_source") or cache.stored_signature("network"),
        notes=notes,
    )
    # Part of the fingerprint: a pickled network built at a different tolerance
    # has different manhole keys and must not be reused.
    signature["snap_tolerance"] = float(config.get("p4_node_snap_tolerance_feet", 0.1) or 0.0)
    log_fn(f"  ML source fingerprint: {signature}")
    _log_fingerprint_notes("ML", signature, notes, log_fn)
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
        notes = []
        source_signature = dataset_signature(
            arcpy, remote, previous=cache.stored_signature(name), notes=notes
        )
        log_fn(f"  {label} source fingerprint: {source_signature}")
        _log_fingerprint_notes(label, source_signature, notes, log_fn)
        local = cache.local_copy(
            arcpy, remote, name, label,
            source_signature,
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
    arcpy = _import_arcpy()
    # Imported here: phase5 imports PHASE4_MAP_OUTPUT_PATH from this module.
    from backend.phase5.run import close_export_worker
    try:
        with _quiet_geoprocessing(arcpy, log_fn), _layer_pool(arcpy) as layer_pool:
            return _run(log_fn, layer_pool=layer_pool)
    finally:
        close_export_worker(log_fn)


def _run(log_fn, layer_pool=None):
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

    ll_status_filter = _check_status_filter(
        arcpy, ll_source, config.get("p4_ll_status_filter"), log_fn
    )
    ml_status_filter = _check_status_filter(
        arcpy, ml_source, config.get("p4_ml_status_filter"), log_fn,
        label="main", config_key="p4_ml_status_filter",
    )
    allowed_mls = None
    if ml_status_filter:
        # Read from the same copy the network was built from, so the OIDs match.
        with arcpy.da.SearchCursor(ml_source, ["OID@"], ml_status_filter) as cursor:
            allowed_mls = {oid for (oid,) in cursor}
        log_fn(f"  {len(allowed_mls)} main(s) match it; the trace will not follow any other")
    _APPEND_PLANS.clear()

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

    _clear_leftover_trace_rows(arcpy, [ml_dest, ll_dest, parcels_dest], log_fn)

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
                    endpoint_index=_endpoint_index_for(geoms), allowed=allowed_mls,
                )
            ml_oids = seed_oids + upstream_traced
            log_fn(f"Traced ML count (incl. seed): {len(ml_oids)}")
            log_fn(f"Traced ML OID preview: {_preview_oids(ml_oids)}")
            # Exclude the seed from the upstream ML trace layer — it belongs only
            # in the HFML layer (already written above via _append_seed_to_hfml_layer).
            upstream_ml_oids = list(upstream_traced)
            with timer.step("append upstream MLs"):
                _append_by_oids(arcpy, upstream_ml_oids, ml_source, ml_dest, log_fn=log_fn)
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
                    downstream_point=downstream_point, log_fn=log_fn, timer=timer,
                    layers=layer_pool, status_filter=ll_status_filter,
                )
            log_fn(f"Selected LL count: {len(ll_oids)}")
            log_fn(f"Selected LL OID preview: {_preview_oids(ll_oids)}")
            with timer.step("append LLs"):
                _append_by_oids(arcpy, ll_oids, ll_source, ll_dest, log_fn=log_fn)
            log_fn(f"Appended {len(ll_oids)} LLs")

            with timer.step("select parcels (remote parcels source)"):
                parcel_oids = _select_parcels_intersecting_lls(
                    arcpy, ll_oids, ll_source, parcels_source,
                    ml_oid_list=ml_oids, ml_source=ml_source, log_fn=log_fn, timer=timer,
                    layers=layer_pool,
                )
            log_fn(f"Selected parcel count: {len(parcel_oids)}")
            log_fn(f"Selected parcel OID preview: {_preview_oids(parcel_oids)}")
            with timer.step("append parcels"):
                _append_by_oids(arcpy, parcel_oids, parcels_source, parcels_dest, log_fn=log_fn)
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

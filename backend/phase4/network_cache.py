"""Local caching for Phase 4's expensive reads.

Phase 4 used to rebuild its directed pipe network once per HFML, each time
selecting every pipe within 50,000 feet of that HFML and reading all of their
geometries over the wire from the ML source (a geodatabase on a network share).
With five HFMLs that is five near-full reads of the gravity main layer per run.

The sewer network barely changes month to month, so this module instead:

  1. copies the ML source to a local file geodatabase, and
  2. builds the directed network once over its full extent and pickles it,

then reuses both until the remote source actually changes.  Staleness is
detected from the source's feature count and the geodatabase's file
timestamps/sizes, with a maximum age as a backstop.

Everything lives under one cache directory, which must be on a LOCAL disk --
pointing it at the share would defeat the purpose.

IMPORTANT -- object IDs:  copying features to a new geodatabase renumbers
OBJECTID.  Phase 4 carries ML object IDs through tracing, appending and (via
the exported record) Phase 5's map export, so once the local copy is in use
every one of those steps must read from the SAME local copy.  Phase 4 does this
by overriding the source paths it passes around, and by handing the overrides to
the map export subprocess in the job record.
"""

import json
import os
import pickle
import shutil
import struct
import tempfile
import time

CACHE_FORMAT_VERSION = 3
LOCAL_GDB_NAME = "hfml_phase4_sources.gdb"
NETWORK_PICKLE_NAME = "directed_network.pickle"
STATE_NAME = "cache_state.json"


def default_cache_dir():
    """A local directory that survives between ArcGIS Pro sessions.

    NOT tempfile.gettempdir(): inside ArcGIS Pro that resolves to a per-session
    folder (...\\Temp\\2\\ArcGISProTempNNNNN), so the cache would be thrown away
    every time Pro restarts and every run would rebuild from scratch.

    NOT the project folder either: for shared users the project itself lives on
    the network share, so caching there would cache onto the very disk we are
    trying to avoid.
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    if os.path.isdir(base):
        return os.path.join(base, "SASD_HFML", "phase4_cache")
    return os.path.join(tempfile.gettempdir(), "hfml_phase4_cache")


def _gdb_root(path):
    """The .gdb (or .sde) folder containing a dataset path, if any."""
    probe = path
    while probe and probe not in (os.path.dirname(probe),):
        if probe.lower().endswith((".gdb", ".sde")):
            return probe
        probe = os.path.dirname(probe)
    return None


# Geometry type codes in a .gdbtable field section, by Describe's shapeType.
# A table without geometry is 0.
_GEOMETRY_CODES = {"point": 1, "multipoint": 2, "polyline": 3, "polygon": 4, "multipatch": 9}


def _read_table_header(path):
    """(row count, field count, geometry type code) from a .gdbtable header.

    Layout as read by GDAL's OpenFileGDB driver (filegdbtable.cpp): an int32
    format version (3, or 4 for ArcGIS Pro 3.2+ 64-bit object IDs), the valid
    row count (int32 at byte 4 in version 3, int64 at byte 16 in version 4), and
    at byte 32 the offset of the field section, which holds the geometry type
    at its byte 8 and the uint16 field count at its byte 12.  None when the file
    cannot be read that way.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(40)
            if len(head) < 40:
                return None
            version = struct.unpack_from("<i", head, 0)[0]
            if version == 3:
                rows = struct.unpack_from("<i", head, 4)[0]
            elif version == 4:
                rows = struct.unpack_from("<q", head, 16)[0]
            else:
                return None
            offset = struct.unpack_from("<Q", head, 32)[0]
            f.seek(offset)
            fields_head = f.read(14)
            if len(fields_head) < 14:
                return None
            geometry = fields_head[8]
            fields = struct.unpack_from("<H", fields_head, 12)[0]
    except (OSError, struct.error, OverflowError, ValueError):
        return None
    if rows < 0:
        return None
    return rows, fields, geometry


def _table_files(arcpy, path, count, hint=None, notes=None):
    """(table ID, files) for this one table in a file geodatabase, or None.

    A file geodatabase stores each table as a{ID}.gdbtable plus index files
    a{ID}.*.  Fingerprinting just those means a change to some other layer in a
    shared geodatabase -- SQL_WEB.gdb holds many and is republished several
    times a day -- no longer forces the mainlines and parcels to be copied
    again.

    A candidate is only accepted when its own header holds exactly this
    dataset's row count, field count and geometry type.  Candidates, in order:
    the ID found last run (`hint`), the ID from Describe's DSID, then every
    table in the geodatabase, where exactly one may match.  Anything less certain returns
    None and the caller falls back to the whole geodatabase: falling back only
    costs a re-copy, while trusting the wrong files could keep a stale copy.
    Why it fell back is appended to `notes`.
    """
    def note(text):
        if notes is not None:
            notes.append(text)

    root = _gdb_root(path)
    if not root or not root.lower().endswith(".gdb") or not os.path.isdir(root):
        note("not in a file geodatabase folder")
        return None
    if not count:
        note("its row count could not be read")
        return None
    try:
        field_count = len(arcpy.ListFields(path))
        described = arcpy.Describe(path)
    except Exception as exc:
        note(f"its fields could not be listed ({exc})")
        return None
    shape_type = str(getattr(described, "shapeType", "") or "").lower()
    geometry = _GEOMETRY_CODES.get(shape_type, 0)
    if shape_type and shape_type not in _GEOMETRY_CODES:
        note(f"unrecognised shape type {shape_type!r}")
        return None
    try:
        entries = [e for e in os.scandir(root) if e.is_file()]
    except OSError as exc:
        note(f"the geodatabase folder could not be listed ({exc})")
        return None

    by_id = {}
    for entry in entries:
        name = entry.name.lower()
        table_id, dot, _ext = name.partition(".")
        if dot and len(table_id) == 9 and table_id[0] == "a":
            by_id.setdefault(table_id, []).append(entry)

    headers = {}

    def header(table_id):
        if table_id not in headers:
            files = by_id.get(table_id, [])
            table = next((e for e in files if e.name.lower().endswith(".gdbtable")), None)
            headers[table_id] = _read_table_header(table.path) if table else None
        return headers[table_id]

    def matches(table_id):
        return header(table_id) == (count, field_count, geometry)

    def describe(found):
        rows, fields, code = found
        kind = next((k for k, v in _GEOMETRY_CODES.items() if v == code), "no" if code == 0 else f"type {code}")
        return f"{rows} rows, {fields} fields and {kind} geometry"

    wanted = describe((count, field_count, geometry))
    if hint and hint in by_id and matches(hint):
        return hint, by_id[hint]
    try:
        dsid = int(getattr(described, "DSID"))
    except Exception:
        dsid = None
    dsid_id = f"a{dsid:08x}" if dsid and dsid > 0 else None
    if dsid_id and matches(dsid_id):
        return dsid_id, by_id[dsid_id]
    if dsid_id is None:
        note("no DSID")
    elif dsid_id not in by_id:
        note(f"DSID {dsid} names no table file ({dsid_id})")
    else:
        found = header(dsid_id)
        note(
            f"DSID {dsid} names {dsid_id}, which holds "
            + (describe(found) if found else "an unreadable header")
            + f", not {wanted}"
        )

    found = sorted(table_id for table_id in by_id if matches(table_id))
    if len(found) == 1:
        return found[0], by_id[found[0]]
    if found:
        note(f"{len(found)} tables have {wanted} ({', '.join(found)}), so which is ours is unclear")
    else:
        unreadable = sum(1 for table_id in by_id if header(table_id) is None)
        note(
            f"none of the geodatabase's {len(by_id)} tables has {wanted}"
            + (f" ({unreadable} header(s) unreadable)" if unreadable else "")
        )
    return None


def dataset_signature(arcpy, path, previous=None, notes=None):
    """A cheap fingerprint of a dataset, used to spot changes in the source.

    Feature count plus the newest timestamp and total size of the files holding
    it: just this table's files when they can be identified (scope "table", its
    file ID under "table"), otherwise the whole geodatabase folder (scope
    "geodatabase").  Reads no feature geometry.

    `previous` is the signature stored last run; its table ID is tried first so
    the geodatabase is only scanned when that table has moved.  How the files
    were found, or why they were not, is appended to `notes`.
    """
    signature = {"count": None, "scope": None, "table": None, "newest_mtime": None, "total_size": None}
    try:
        signature["count"] = int(arcpy.management.GetCount(path)[0])
    except Exception:
        pass

    hint = (previous or {}).get("table")
    found = _table_files(arcpy, path, signature["count"], hint=hint, notes=notes)
    if found:
        table_id, table_files = found
        try:
            signature["newest_mtime"] = round(max(e.stat().st_mtime for e in table_files), 3)
            signature["total_size"] = sum(e.stat().st_size for e in table_files)
            signature["scope"] = "table"
            signature["table"] = table_id
            return signature
        except OSError as exc:
            if notes is not None:
                notes.append(f"its table files could not be read ({exc})")

    root = _gdb_root(path)
    if root and os.path.isdir(root):
        newest = 0.0
        total = 0
        try:
            for entry in os.scandir(root):
                if entry.is_file():
                    stat = entry.stat()
                    newest = max(newest, stat.st_mtime)
                    total += stat.st_size
        except OSError:
            newest, total = 0.0, 0
        if total:
            signature["newest_mtime"] = round(newest, 3)
            signature["total_size"] = total
            signature["scope"] = "geodatabase"
    return signature


def free_gb(path):
    """Free space in GB on the disk holding `path` (its nearest existing parent)."""
    probe = os.path.abspath(path)
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent
    try:
        return shutil.disk_usage(probe).free / (1024.0 ** 3)
    except OSError:
        return None


def _tree_size_gb(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total / (1024.0 ** 3)


def signature_is_weak(signature):
    """True when the fingerprint is only a feature count.

    An edit that leaves the count unchanged would then go unnoticed, so the
    maximum cache age is the only protection and the caller should say so.
    """
    return signature.get("total_size") is None


class Phase4Cache:
    def __init__(self, cache_dir, max_age_days, log_fn):
        self.dir = os.path.abspath(cache_dir)
        self.max_age_days = max_age_days
        self.log_fn = log_fn
        self.state_path = os.path.join(self.dir, STATE_NAME)
        self.gdb_path = os.path.join(self.dir, LOCAL_GDB_NAME)
        self.network_path = os.path.join(self.dir, NETWORK_PICKLE_NAME)

    # -- state ---------------------------------------------------------------
    def _read_state(self):
        try:
            with open(self.state_path, "r") as f:
                state = json.load(f)
        except Exception:
            return {}
        if state.get("format_version") != CACHE_FORMAT_VERSION:
            return {}
        return state

    def _write_state(self, state):
        state["format_version"] = CACHE_FORMAT_VERSION
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, self.state_path)

    def _too_old(self, entry):
        if not self.max_age_days:
            return False
        written = entry.get("written_at", 0)
        age_days = (time.time() - written) / 86400.0
        return age_days > self.max_age_days

    def _entry_is_fresh(self, entry, source_path, signature):
        if not entry:
            return False
        if entry.get("source_path") != source_path:
            return False
        if entry.get("signature") != signature:
            return False
        if self._too_old(entry):
            self.log_fn(
                f"  Cache entry is older than {self.max_age_days} day(s) — rebuilding"
            )
            return False
        return True

    def stored_signature(self, name):
        """The source signature recorded with the local copy `name` (or with
        the network, for "network"), or None."""
        key = name if name == "network" else f"copy:{name}"
        entry = self._read_state().get(key) or {}
        return entry.get("signature")

    def clear(self):
        """Drop the whole cache directory.  Used when a rebuild fails midway."""
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- local copy of the ML source ----------------------------------------
    def local_copy(self, arcpy, source, name, label, signature, min_free_gb=0):
        """Return a local path to a copy of `source`, reusing it when fresh.

        `name` is the feature class name inside the local cache geodatabase and
        the key this copy is tracked under.  Returns None when the copy cannot
        be made -- no disk space, an unreadable source, a failed copy -- and the
        caller then carries on against the remote source unchanged.

        Object IDs: a copy renumbers OBJECTID, so whoever takes this path must
        use it for every step that carries object IDs for this dataset.
        """
        state_key = f"copy:{name}"
        state = self._read_state()
        entry = state.get(state_key)
        dest = os.path.join(self.gdb_path, name)

        if self._entry_is_fresh(entry, source, signature):
            try:
                if arcpy.Exists(dest):
                    self.log_fn(f"  Reusing local {label} copy: {dest}")
                    return dest
            except Exception:
                pass

        os.makedirs(self.dir, exist_ok=True)

        if min_free_gb:
            available = free_gb(self.dir)
            if available is None:
                self.log_fn(
                    f"  WARNING: could not read free disk space for {self.dir} — "
                    f"not copying the {label} source locally"
                )
                return None
            if available < min_free_gb:
                self.log_fn(
                    f"  Not copying the {label} source locally: {available:.1f} GB free, "
                    f"{min_free_gb:.1f} GB required (p4_cache_min_free_gb). "
                    "Continuing against the remote source."
                )
                return None

        try:
            if not arcpy.Exists(self.gdb_path):
                arcpy.management.CreateFileGDB(self.dir, LOCAL_GDB_NAME)
            if arcpy.Exists(dest):
                arcpy.management.Delete(dest)
            started = time.time()
            self.log_fn(f"  Copying {label} source to local disk ({self.dir})...")
            before_gb = _tree_size_gb(self.gdb_path) if os.path.isdir(self.gdb_path) else 0.0
            arcpy.management.CopyFeatures(source, dest)
            count = int(arcpy.management.GetCount(dest)[0])
            grew_gb = max(0.0, _tree_size_gb(self.gdb_path) - before_gb)
            self.log_fn(
                f"  Copied {count} {label} features locally in {time.time() - started:.1f}s "
                f"(+{grew_gb:.2f} GB, {free_gb(self.dir):.1f} GB now free)"
            )
        except Exception as exc:
            self.log_fn(f"  WARNING: could not copy the {label} source locally — {exc}")
            self.log_fn(f"  Continuing against the remote {label} source.")
            # A half-written copy is worse than none: it would be reused as if
            # complete on the next run.
            try:
                if arcpy.Exists(dest):
                    arcpy.management.Delete(dest)
            except Exception:
                pass
            state.pop(state_key, None)
            self._write_state(state)
            return None

        if name == "ml_source":
            # The copy renumbers OBJECTID, so a pickled network built from the
            # previous copy no longer matches and must not be reused.
            state.pop("network", None)
        state[state_key] = {
            "source_path": source,
            "signature": signature,
            "written_at": time.time(),
            "local_path": dest,
        }
        self._write_state(state)
        return dest

    def local_ml_copy(self, arcpy, ml_source, signature, min_free_gb=0):
        return self.local_copy(
            arcpy, ml_source, "ml_source", "ML", signature, min_free_gb=min_free_gb
        )

    # -- directed network ----------------------------------------------------
    def load_network(self, network_source, signature):
        """Return a cached directed network tuple, or None."""
        state = self._read_state()
        entry = state.get("network")
        if not self._entry_is_fresh(entry, network_source, signature):
            return None
        if not os.path.exists(self.network_path):
            return None
        try:
            started = time.time()
            with open(self.network_path, "rb") as f:
                payload = pickle.load(f)
            self.log_fn(
                f"  Loaded cached network ({payload['feature_count']} features) "
                f"in {time.time() - started:.1f}s from {self.network_path}"
            )
            return (
                payload["directed_net"],
                payload["geoms"],
                payload["pipe_ups"],
                payload["pipe_dns"],
                payload["use_nodes"],
            )
        except Exception as exc:
            self.log_fn(f"  WARNING: cached network could not be read — {exc}")
            return None

    def save_network(self, network_source, signature, network):
        directed_net, geoms, pipe_ups, pipe_dns, use_nodes = network
        payload = {
            "directed_net": directed_net,
            "geoms": geoms,
            "pipe_ups": pipe_ups,
            "pipe_dns": pipe_dns,
            "use_nodes": use_nodes,
            "feature_count": len(geoms),
        }
        try:
            os.makedirs(self.dir, exist_ok=True)
            tmp = self.network_path + ".tmp"
            with open(tmp, "wb") as f:
                pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, self.network_path)
            size_mb = os.path.getsize(self.network_path) / (1024.0 * 1024.0)
            self.log_fn(f"  Cached network to {self.network_path} ({size_mb:.1f} MB)")
        except Exception as exc:
            self.log_fn(f"  WARNING: could not cache the network — {exc}")
            return

        state = self._read_state()
        state["network"] = {
            "source_path": network_source,
            "signature": signature,
            "written_at": time.time(),
        }
        self._write_state(state)

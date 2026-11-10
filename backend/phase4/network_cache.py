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
import tempfile
import time

CACHE_FORMAT_VERSION = 2
LOCAL_GDB_NAME = "hfml_phase4_sources.gdb"
NETWORK_PICKLE_NAME = "directed_network.pickle"
STATE_NAME = "cache_state.json"


def default_cache_dir():
    """A local scratch directory.

    Deliberately NOT under the project folder: for shared users the project
    itself lives on the network share, so caching there would cache onto the
    very disk we are trying to avoid.
    """
    return os.path.join(tempfile.gettempdir(), "hfml_phase4_cache")


def _gdb_root(path):
    """The .gdb (or .sde) folder containing a dataset path, if any."""
    probe = path
    while probe and probe not in (os.path.dirname(probe),):
        if probe.lower().endswith((".gdb", ".sde")):
            return probe
        probe = os.path.dirname(probe)
    return None


def dataset_signature(arcpy, path):
    """A cheap fingerprint of a dataset, used to spot changes in the source.

    Feature count plus, for a file geodatabase, the newest file timestamp and
    the total size of the geodatabase folder.  Reads no feature geometry.
    """
    signature = {"count": None, "newest_mtime": None, "total_size": None}
    try:
        signature["count"] = int(arcpy.management.GetCount(path)[0])
    except Exception:
        pass

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
    return signature


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

    def clear(self):
        """Drop the whole cache directory.  Used when a rebuild fails midway."""
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- local copy of the ML source ----------------------------------------
    def local_ml_copy(self, arcpy, ml_source, signature):
        """Return a local path to a copy of ml_source, reusing it when fresh.

        Returns None when the copy cannot be made, in which case the caller
        should carry on using the remote source.
        """
        state = self._read_state()
        entry = state.get("ml_copy")
        dest = os.path.join(self.gdb_path, "ml_source")

        if self._entry_is_fresh(entry, ml_source, signature):
            try:
                if arcpy.Exists(dest):
                    self.log_fn(f"  Reusing local ML copy: {dest}")
                    return dest
            except Exception:
                pass

        os.makedirs(self.dir, exist_ok=True)
        try:
            if not arcpy.Exists(self.gdb_path):
                arcpy.management.CreateFileGDB(self.dir, LOCAL_GDB_NAME)
            if arcpy.Exists(dest):
                arcpy.management.Delete(dest)
            started = time.time()
            self.log_fn(f"  Copying ML source to local disk ({self.dir})...")
            arcpy.management.CopyFeatures(ml_source, dest)
            count = int(arcpy.management.GetCount(dest)[0])
            self.log_fn(
                f"  Copied {count} ML features locally in {time.time() - started:.1f}s"
            )
        except Exception as exc:
            self.log_fn(f"  WARNING: could not copy the ML source locally — {exc}")
            self.log_fn("  Continuing against the remote ML source.")
            return None

        # The copy renumbers OBJECTID, so any pickled network built from the
        # previous copy no longer matches and must not be reused.
        state.pop("network", None)
        state["ml_copy"] = {
            "source_path": ml_source,
            "signature": signature,
            "written_at": time.time(),
            "local_path": dest,
        }
        self._write_state(state)
        return dest

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

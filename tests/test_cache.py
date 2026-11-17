import os, sys, types, shutil, tempfile, time
import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]

from backend.phase4.run import _assetnum_where_clause, _read_assetnum_segments
from backend.phase4.network_cache import Phase4Cache, dataset_signature, signature_is_weak

class Field:
    def __init__(self, name, type_): self.name, self.type = name, type_

class Cur:
    def __init__(self, rows): self.rows = rows
    def __enter__(self): return iter(self.rows)
    def __exit__(self, *a): return False

class FakeArcpy:
    def __init__(self, field_type="String", rows=None, count=7):
        self._ft = field_type
        self._rows = rows or []
        self.queries = []
        self.count = count
        self.da = types.SimpleNamespace(SearchCursor=self._search)
        self.management = types.SimpleNamespace(
            GetCount=lambda p: [str(self.count)],
            CreateFileGDB=lambda *a: None,
            CopyFeatures=lambda *a: None,
            Delete=lambda *a: None,
        )
    def ListFields(self, ds): return [Field("MXASSETNUM", self._ft), Field("OBJECTID", "OID")]
    def AddFieldDelimiters(self, ds, f): return f
    def Exists(self, p): return True
    def _search(self, ds, fields, where=None):
        self.queries.append(where)
        if where is None:
            return Cur(self._rows)
        return Cur([r for r in self._rows if str(r[0]) in where or f"'{r[0]}'" in where])

log = lambda m: None
fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond: fails.append(name)

# --- where clause ----------------------------------------------------------
a = FakeArcpy("String")
c = _assetnum_where_clause(a, "src", {"500123", "500124"})
check("text clause lists both spellings",
      c == "MXASSETNUM IN ('500123', '500123.0', '500124', '500124.0')", c)

a = FakeArcpy("Integer")
c = _assetnum_where_clause(a, "src", {"500123", "500124.0"})
check("numeric clause uses ints", c == "MXASSETNUM IN (500123, 500124)", c)

a = FakeArcpy("Integer")
check("non-numeric against numeric field -> no clause",
      _assetnum_where_clause(a, "src", {"ABC-1"}) is None)

a = FakeArcpy("String")
c = _assetnum_where_clause(a, "src", {"O'Brien"})
check("quotes are escaped", c == "MXASSETNUM IN ('O''Brien', 'O''Brien.0')", c)

check("empty set -> no clause", _assetnum_where_clause(FakeArcpy(), "src", set()) is None)

a = FakeArcpy("String")
check("oversized clause -> no clause",
      _assetnum_where_clause(a, "src", {str(i) for i in range(5000)}) is None)

# --- scan + fallback -------------------------------------------------------
rows = [("500123", "geomA", 11), ("500124", "geomB", 12), ("999999", "geomC", 13)]
a = FakeArcpy("String", rows)
seg = _read_assetnum_segments(a, "src", {"500123", "500124"}, {}, log)
check("missing config key -> filter on (matches config_loader default)",
      a.queries == ["MXASSETNUM IN ('500123', '500123.0', '500124', '500124.0')"], a.queries)
check("filtered scan finds both", set(seg) == {"500123", "500124"}, seg)

a = FakeArcpy("String", rows)
seg = _read_assetnum_segments(a, "src", {"500123", "500124"}, {"p4_filter_assetnum_scan": False}, log)
check("filter off -> full scan, no clause", a.queries == [None], a.queries)
check("full scan finds both", set(seg) == {"500123", "500124"}, seg)

a = FakeArcpy("String", rows)
seg = _read_assetnum_segments(a, "src", {"500123"}, {"p4_filter_assetnum_scan": True}, log)
check("filtered scan sends a clause", a.queries[0] is not None, a.queries)
check("filtered scan finds it", set(seg) == {"500123"}, seg)

# clause that matches nothing -> falls back to a full scan
class MissArcpy(FakeArcpy):
    def _search(self, ds, fields, where=None):
        self.queries.append(where)
        return Cur([] if where is not None else self._rows)
a = MissArcpy("String", rows)
seg = _read_assetnum_segments(a, "src", {"500123"}, {"p4_filter_assetnum_scan": True}, log)
check("empty filtered result falls back to full scan", a.queries == [a.queries[0], None], a.queries)
check("fallback still finds it", set(seg) == {"500123"}, seg)

# geometry-less rows are skipped, as before
a = FakeArcpy("String", [("500123", None, 11)])
seg = _read_assetnum_segments(a, "src", {"500123"}, {}, log)
check("null geometry skipped", seg == {}, seg)

# --- cache -----------------------------------------------------------------
d = tempfile.mkdtemp()
try:
    cache = Phase4Cache(os.path.join(d, "c"), 7, log)
    net = ({("a",): [(1, ("b",))]}, {1: (0.0, 1.0, 2.0, 3.0)}, {1: ("b",)}, {1: ("a",)}, False)
    sig = {"count": 10, "newest_mtime": 1.0, "total_size": 99}

    check("no cache -> load returns None", cache.load_network("src", sig) is None)
    cache.save_network("src", sig, net)
    check("saved network round-trips", cache.load_network("src", sig) == net)
    check("changed signature -> miss",
          cache.load_network("src", dict(sig, count=11)) is None)
    check("different source path -> miss", cache.load_network("other", sig) is None)

    old = Phase4Cache(cache.dir, 7, log)
    state = old._read_state()
    state["network"]["written_at"] = time.time() - 30 * 86400
    old._write_state(state)
    check("too old -> miss", old.load_network("src", sig) is None)

    state["network"]["written_at"] = time.time() - 30 * 86400
    old._write_state(state)
    forever = Phase4Cache(cache.dir, 0, log)
    check("max_age 0 disables the age check", forever.load_network("src", sig) == net)

    bad = Phase4Cache(os.path.join(d, "c"), 7, log)
    with open(bad.network_path, "wb") as f: f.write(b"not a pickle")
    check("corrupt pickle -> None, no crash", bad.load_network("src", sig) is None)

    # a fresh local copy must invalidate a network built from the previous one
    cache.save_network("src", sig, net)
    cache.local_ml_copy(FakeArcpy(), "remote", sig)
    check("new local copy drops the cached network",
          cache._read_state().get("network") is None, cache._read_state())

    check("weak signature detected", signature_is_weak({"count": 5, "total_size": None}))
    check("full signature not weak", not signature_is_weak(sig))

    # a failed copy must not take the pipeline down
    class BrokenArcpy(FakeArcpy):
        def __init__(self):
            super().__init__()
            self.management.CopyFeatures = self._boom
        def _boom(self, *a): raise RuntimeError("share unreachable")
    check("copy failure returns None (caller keeps remote source)",
          Phase4Cache(os.path.join(d, "c2"), 7, log).local_ml_copy(BrokenArcpy(), "remote", sig) is None)
finally:
    shutil.rmtree(d, ignore_errors=True)

print()
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)

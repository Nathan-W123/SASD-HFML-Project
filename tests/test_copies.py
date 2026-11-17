import sys, os, types, shutil, tempfile
import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
from backend.phase4.network_cache import Phase4Cache, free_gb

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond: fails.append(name)

class FakeArcpy:
    def __init__(self, fail_on=None):
        self.copied = []
        self.deleted = []
        self.fail_on = fail_on or set()
        self.exists = set()
        self.management = types.SimpleNamespace(
            GetCount=lambda p: ["42"],
            CreateFileGDB=lambda d, n: self.exists.add(os.path.join(d, n)),
            CopyFeatures=self._copy,
            Delete=self._delete,
        )
    def _copy(self, src, dest):
        if src in self.fail_on:
            raise RuntimeError("source unreachable")
        self.copied.append((src, dest))
        self.exists.add(dest)
    def _delete(self, p):
        self.deleted.append(p)
        self.exists.discard(p)
    def Exists(self, p): return p in self.exists

SIG = {"count": 10, "newest_mtime": 1.0, "total_size": 99}
log = lambda m: lines.append(m)

d = tempfile.mkdtemp()
try:
    lines = []
    c = Phase4Cache(os.path.join(d, "c"), 7, log)
    a = FakeArcpy()

    ll = c.local_copy(a, r"S:\x.gdb\lat", "ll_source", "LL", SIG)
    check("copies the LL source", ll and ll.endswith("ll_source"), ll)
    check("logged the label", any("Copying LL source" in l for l in lines), lines)
    check("logged size and free space", any("GB," in l and "now free" in l for l in lines), lines)

    lines.clear()
    again = c.local_copy(a, r"S:\x.gdb\lat", "ll_source", "LL", SIG)
    check("reuses a fresh copy", again == ll, again)
    check("reuse does not re-copy", len(a.copied) == 1, a.copied)
    check("reuse is logged", any("Reusing local LL copy" in l for l in lines), lines)

    # each dataset is tracked separately
    pa = c.local_copy(a, r"\\pw\x.gdb\parcels", "parcels_source", "parcels", SIG)
    check("parcels tracked separately", pa and pa.endswith("parcels_source"), pa)
    check("both copies recorded", len(a.copied) == 2, a.copied)

    # a changed source signature forces a re-copy of that one only
    lines.clear()
    c.local_copy(a, r"S:\x.gdb\lat", "ll_source", "LL", dict(SIG, count=11))
    check("changed signature re-copies", len(a.copied) == 3, a.copied)
    check("parcels copy still reused",
          c.local_copy(a, r"\\pw\x.gdb\parcels", "parcels_source", "parcels", SIG) == pa)

    # the ML copy must still invalidate the pickled network; others must not
    c.save_network("src", SIG, ({}, {}, {}, {}, False))
    c.local_copy(a, r"S:\x.gdb\lat", "ll_source", "LL", dict(SIG, count=12))
    check("an LL copy leaves the network cache alone",
          c._read_state().get("network") is not None)
    c.local_ml_copy(a, r"\\pw\x.gdb\ml", dict(SIG, count=13))
    check("an ML copy drops the network cache",
          c._read_state().get("network") is None)

    # disk guard
    lines.clear()
    c2 = Phase4Cache(os.path.join(d, "c2"), 7, log)
    out = c2.local_copy(a, r"\\pw\x.gdb\parcels", "parcels_source", "parcels", SIG,
                        min_free_gb=10 ** 9)
    check("skips the copy when space is short", out is None, out)
    check("says why it skipped",
          any("Not copying the parcels source locally" in l and "GB free" in l for l in lines), lines)

    out = c2.local_copy(a, r"\\pw\x.gdb\parcels", "parcels_source", "parcels", SIG, min_free_gb=0.000001)
    check("copies when there is room", out is not None, out)

    # a failed copy must clean up and leave no state to reuse
    lines.clear()
    c3 = Phase4Cache(os.path.join(d, "c3"), 7, log)
    bad = FakeArcpy(fail_on={r"S:\gone.gdb\lat"})
    out = c3.local_copy(bad, r"S:\gone.gdb\lat", "ll_source", "LL", SIG)
    check("failed copy returns None", out is None, out)
    check("failed copy warns", any("could not copy the LL source" in l for l in lines), lines)
    check("failed copy leaves no state", c3._read_state().get("copy:ll_source") is None,
          c3._read_state())

    check("free_gb reads a real path", (free_gb(d) or 0) > 0, free_gb(d))
    check("free_gb handles a missing path", free_gb(os.path.join(d, "no", "such")) is not None)
finally:
    shutil.rmtree(d, ignore_errors=True)

print()
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)

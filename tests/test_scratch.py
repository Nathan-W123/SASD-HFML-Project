import sys, types
import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
import backend.phase4.run as R

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond: fails.append(name)

class FakeArcpy:
    def __init__(self, memory_ok=True):
        self.memory_ok = memory_ok
        self.created = []
        self.exists = set()
        self.management = types.SimpleNamespace(
            CreateFeatureclass=self._create,
            Delete=lambda p: self.exists.discard(p),
            AddField=lambda *a: None,
        )
    def _create(self, ws, name, kind, spatial_reference=None):
        if ws == "memory" and not self.memory_ok:
            raise RuntimeError("memory workspace not supported")
        self.created.append((ws, name))
        self.exists.add(f"{ws}\\{name}")
    def Exists(self, p): return p in self.exists

R._SCRATCH_WORKSPACE.clear()
lines = []
a = FakeArcpy(memory_ok=True)
check("prefers memory", R._scratch_workspace(a, lines.append) == "memory", lines)
check("logs which workspace", any("Scratch workspace: memory" in l for l in lines), lines)
calls = len(a.created)
R._scratch_workspace(a, lines.append)
check("probes only once", len(a.created) == calls, a.created)

R._SCRATCH_WORKSPACE.clear()
lines = []
b = FakeArcpy(memory_ok=False)
check("falls back to in_memory", R._scratch_workspace(b, lines.append) == "in_memory", lines)
check("says why it fell back",
      any("unavailable" in l and "in_memory" in l for l in lines), lines)

# the probe must clean up after itself
R._SCRATCH_WORKSPACE.clear()
c = FakeArcpy(memory_ok=True)
R._scratch_workspace(c)
check("probe leaves nothing behind", c.exists == set(), c.exists)

print()
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)

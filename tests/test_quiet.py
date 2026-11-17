import sys, types
import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
from backend.phase4.run import _quiet_geoprocessing

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond: fails.append(name)

def make_arcpy(history=True, metadata=True, add=True, has_metadata=True, has_env=True):
    state = {"history": history, "metadata": metadata}
    a = types.SimpleNamespace()
    a.GetLogHistory = lambda: state["history"]
    a.SetLogHistory = lambda v: state.__setitem__("history", v)
    if has_metadata:
        a.GetLogMetadata = lambda: state["metadata"]
        a.SetLogMetadata = lambda v: state.__setitem__("metadata", v)
    if has_env:
        a.env = types.SimpleNamespace(addOutputsToMap=add)
    a._state = state
    return a

# all three switched off inside, restored after
a = make_arcpy()
lines = []
with _quiet_geoprocessing(a, lines.append):
    inside = (a._state["history"], a._state["metadata"], a.env.addOutputsToMap)
check("all off inside", inside == (False, False, False), inside)
after = (a._state["history"], a._state["metadata"], a.env.addOutputsToMap)
check("all restored after", after == (True, True, True), after)
check("logs what it switched",
      any("history logging" in l and "metadata logging" in l and "restored afterwards" in l
          for l in lines), lines)

# restores on failure, and the exception still propagates
a = make_arcpy()
raised = False
try:
    with _quiet_geoprocessing(a):
        raise RuntimeError("phase 4 blew up")
except RuntimeError:
    raised = True
check("exception propagates", raised)
check("restored after failure",
      (a._state["history"], a._state["metadata"], a.env.addOutputsToMap) == (True, True, True))

# restores the user's actual previous values, not blanket True
a = make_arcpy(history=False, metadata=True, add=False)
with _quiet_geoprocessing(a):
    pass
check("restores prior values exactly",
      (a._state["history"], a._state["metadata"], a.env.addOutputsToMap) == (False, True, False),
      (a._state, a.env.addOutputsToMap))

# older Pro without SetLogMetadata: skip it, do the rest
a = make_arcpy(has_metadata=False)
lines = []
with _quiet_geoprocessing(a, lines.append):
    check("history still switched without metadata API", a._state["history"] is False)
check("metadata not claimed when absent",
      not any("metadata logging" in l for l in lines), lines)

# nothing switchable at all: no crash, says so
bare = types.SimpleNamespace()
lines = []
with _quiet_geoprocessing(bare, lines.append):
    pass
check("bare arcpy tolerated", any("no switchable settings" in l for l in lines), lines)

# a setter that raises on restore must not mask the run's result
a = make_arcpy()
calls = {"n": 0}
def flaky(v):
    calls["n"] += 1
    if calls["n"] > 1:
        raise OSError("project locked")
    a._state["history"] = v
a.SetLogHistory = flaky
lines = []
with _quiet_geoprocessing(a, lines.append):
    pass
check("failed restore is reported, not raised",
      any("could not restore history logging" in l for l in lines), lines)

print()
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)

import sys
import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
from backend.phase4.run import (
    _NodeSnapper, _build_point_index, _near_points, _endpoint_index,
    _report_network_gaps, _hfml_ends, _trace_upstream_directed,
    GAP_REPORT_RADIUS_FEET,
)

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond: fails.append(name)

# ---- snapper --------------------------------------------------------------
s = _NodeSnapper(0.1)
a = s.key(100.0, 200.0)
b = s.key(100.01, 200.0)          # 0.01 ft away -> same manhole
c = s.key(100.5, 200.0)           # 0.5 ft away -> its own manhole
check("near ends share a key", a == b, (a, b))
check("far ends do not", a != c, (a, c))
check("snapped counter", s.snapped == 1, s.snapped)

s0 = _NodeSnapper(0)
check("tolerance 0 keeps exact matching",
      s0.key(100.0, 200.0) != s0.key(100.01, 200.0))
check("tolerance 0 still rounds to 3dp",
      s0.key(1.00049, 2.0) == s0.key(1.0, 2.0))

# a chain of ends each 0.06 ft from the last must not snowball into one node
s = _NodeSnapper(0.1)
keys = [s.key(100.0 + i * 0.06, 200.0) for i in range(5)]
check("chained ends do not all collapse", len(set(keys)) > 1, keys)

# across grid cell boundaries
s = _NodeSnapper(0.1)
check("snaps across a cell boundary",
      s.key(99.999, 200.0) == s.key(100.001, 200.0))

# ---- point index ----------------------------------------------------------
idx = _build_point_index([(0.0, 0.0, "a"), (1.0, 0.0, "b"), (50.0, 0.0, "c")], 2.0)
near = _near_points(idx, 2.0, 0.0, 0.0, 2.0)
check("index finds points in radius, nearest first",
      [n[3] for n in near] == ["a", "b"], near)
check("index excludes points beyond radius",
      "c" not in [n[3] for n in _near_points(idx, 2.0, 0.0, 0.0, 2.0)])

ei = _endpoint_index({7: (0.0, 0.0, 10.0, 0.0)})
check("endpoint index holds both ends of a pipe",
      sorted(n[3] for n in _near_points(ei, GAP_REPORT_RADIUS_FEET, 10.0, 0.0, 0.5)) == [7])

# ---- gap histogram (read-only, must not raise) ---------------------------
lines = []
_report_network_gaps({(0.0, 0.0), (0.02, 0.0), (500.0, 0.0)}, lines.append)
check("gap report names the close pair", any("0.0200" in l for l in lines), lines)
lines = []
_report_network_gaps({(0.0, 0.0), (500.0, 0.0)}, lines.append)
check("gap report says snapped when nothing is close",
      any("looks snapped" in l for l in lines), lines)
lines = []
_report_network_gaps(set(), lines.append)
check("gap report handles an empty network", lines == [], lines)
lines = []
_report_network_gaps({"NODE1", "NODE2"}, lines.append)
check("gap report skips node-ID keys", lines == [], lines)

# ---- direction tie-break -------------------------------------------------
# Seed 1 is digitized A->B, so A looks upstream. Nothing drains out of either
# end, and pipe 2 arrives at B. Upstream pipes flow INTO the upstream manhole,
# so B is really the upstream end and the HFML was drawn backwards.
A, B, C = (0.0, 0.0), (10.0, 0.0), (-10.0, 0.0)
pipe_ups = {1: A, 2: C}
pipe_dns = {1: B, 2: B}
directed = {B: [(1, A), (2, C)]}
outflows = {}
for oid, key in pipe_ups.items():
    outflows.setdefault(key, []).append(oid)
up, dn = _hfml_ends([1], pipe_ups, pipe_dns, outflows, True, lambda m: None, directed_net=directed)
check("flips when pipes arrive only at the digitized downstream end",
      (up, dn) == (B, A), (up, dn))

# The flipped end must then actually trace those pipes.
lines = []
traced, _ = _trace_upstream_directed([1], directed, pipe_ups, pipe_dns,
                                     infer_direction=True, log_fn=lines.append)
check("the flip makes the upstream pipe traceable", traced == [2], traced)

# Correctly oriented: pipe 2 already arrives at the digitized upstream end,
# so nothing should move.
pipe_ups = {1: A, 2: C}
pipe_dns = {1: B, 2: A}
directed = {B: [(1, A)], A: [(2, C)]}
outflows = {}
for oid, key in pipe_ups.items():
    outflows.setdefault(key, []).append(oid)
up, dn = _hfml_ends([1], pipe_ups, pipe_dns, outflows, True, lambda m: None, directed_net=directed)
check("leaves a correctly oriented HFML alone", (up, dn) == (A, B), (up, dn))

# A genuine top-of-system HFML: pipe 2 leaves B downstream. Must NOT flip.
pipe_ups = {1: A, 2: B}
pipe_dns = {1: B, 2: (20.0, 0.0)}
directed = {B: [(1, A)], (20.0, 0.0): [(2, B)]}
outflows = {}
for oid, key in pipe_ups.items():
    outflows.setdefault(key, []).append(oid)
up, dn = _hfml_ends([1], pipe_ups, pipe_dns, outflows, True, lambda m: None, directed_net=directed)
check("does not flip a real top-of-system HFML", (up, dn) == (A, B), (up, dn))

# Inflows at both ends -> ambiguous, leave the digitized order alone
pipe_ups = {1: A, 2: C, 3: (20.0, 0.0)}
pipe_dns = {1: B, 2: A, 3: B}
directed = {B: [(1, A), (3, (20.0, 0.0))], A: [(2, C)]}
outflows = {}
for oid, key in pipe_ups.items():
    outflows.setdefault(key, []).append(oid)
up, dn = _hfml_ends([1], pipe_ups, pipe_dns, outflows, True, lambda m: None, directed_net=directed)
check("ambiguous inflows leave direction as digitized", (up, dn) == (A, B), (up, dn))

check("no directed_net passed -> old behaviour, no crash",
      _hfml_ends([1], pipe_ups, pipe_dns, outflows, True, lambda m: None) == (A, B))

# ---- frontier gap report -------------------------------------------------
# Seed 1 runs A(up)->B(dn). Pipe 9 should feed A but its end is 0.02 ft off.
pipe_ups = {1: A, 9: (-10.0, 0.0)}
pipe_dns = {1: B, 9: (0.02, 0.0)}
directed = {B: [(1, A)], (0.02, 0.0): [(9, (-10.0, 0.0))]}
geoms = {1: (0.0, 0.0, 10.0, 0.0), 9: (-10.0, 0.0, 0.02, 0.0)}
lines = []
traced, dn_key = _trace_upstream_directed(
    [1], directed, pipe_ups, pipe_dns, infer_direction=True,
    log_fn=lines.append, endpoint_index=_endpoint_index(geoms),
)
check("gap stops the trace (unchanged behaviour)", traced == [], traced)
check("frontier report names the gap OID", any("OID 9" in l for l in lines), lines)
check("frontier report gives the distance", any("0.0200 ft away" in l for l in lines), lines)

# Properly snapped: no gap reported, and the pipe is traced
pipe_dns = {1: B, 9: A}
directed = {B: [(1, A)], A: [(9, (-10.0, 0.0))]}
geoms = {1: (0.0, 0.0, 10.0, 0.0), 9: (-10.0, 0.0, 0.0, 0.0)}
lines = []
traced, _ = _trace_upstream_directed(
    [1], directed, pipe_ups, pipe_dns, infer_direction=True,
    log_fn=lines.append, endpoint_index=_endpoint_index(geoms),
)
check("snapped network traces the pipe", traced == [9], traced)
check("no gap reported when nothing is dangling",
      not any("unconnected pipe end" in l for l in lines), lines)

# diagnostic off -> no report, same trace result
lines = []
traced2, _ = _trace_upstream_directed(
    [1], directed, pipe_ups, pipe_dns, infer_direction=True,
    log_fn=lines.append, endpoint_index=None,
)
check("index=None disables the report", traced2 == [9] and not any("NOTE" in l and "unconnected" in l for l in lines))

print()
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)

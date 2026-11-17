import importlib.util, random, sys, types
import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
import stub_arcpy as S
import backend.phase4.run as NEW
from baseline import load_baseline
OLD = load_baseline()
if OLD is None:
    print("SKIP: needs the pre-rewrite Phase 4 code from git history (see baseline.py)")
    print("FAILURES: none")
    sys.exit(0)

fails = []
def check(name, cond, detail=""):
    if not cond:
        print("FAIL " + name + " :: " + str(detail)); fails.append(name)

def network(pipes):
    """pipes: {oid: (up_key, dn_key)} -> directed_net, pipe_ups, pipe_dns"""
    net, ups, dns = {}, {}, {}
    for oid, (u, d) in pipes.items():
        ups[oid], dns[oid] = u, d
        net.setdefault(d, []).append((oid, u))
    return net, ups, dns

def random_pipes(rnd, n):
    pipes, oid = {}, 100
    for i in range(1, n):
        p = rnd.randrange(0, i)
        oid += rnd.randint(1, 3)
        u, d = (i, 0), (p, 0)
        if rnd.random() < 0.15:          # digitized against the flow
            u, d = d, u
        pipes[oid] = (u, d)
    for _ in range(rnd.randint(0, 4)):   # a few loops / cross-connections
        a, b = rnd.randrange(n), rnd.randrange(n)
        if a != b:
            oid += 1
            pipes[oid] = ((a, 0), (b, 0))
    return pipes

# ---- 1. no filter: identical to the old trace ------------------------------
compared = traced_total = 0
for seed in range(2000):
    rnd = random.Random(seed)
    pipes = random_pipes(rnd, rnd.randint(4, 60))
    net, ups, dns = network(pipes)
    seed_oid = rnd.choice(sorted(pipes))
    old = OLD._trace_upstream_directed([seed_oid], net, ups, dns, infer_direction=True, log_fn=None)
    new = NEW._trace_upstream_directed([seed_oid], net, ups, dns, infer_direction=True, log_fn=None)
    check(f"net {seed}: no filter == old", new == old, (new, old))
    everything = NEW._trace_upstream_directed([seed_oid], net, ups, dns, infer_direction=True,
                                              log_fn=None, allowed=set(pipes))
    check(f"net {seed}: all-allowed == old", everything == old, (everything, old))
    compared += 1; traced_total += len(old[0])
print(f"{compared} random networks: trace identical to the old code with no filter ({traced_total} pipes)")

# ---- 2. with a filter: never maps a non-operating main ----------------------
for seed in range(2000):
    rnd = random.Random(seed)
    pipes = random_pipes(rnd, rnd.randint(4, 60))
    net, ups, dns = network(pipes)
    seed_oid = rnd.choice(sorted(pipes))
    allowed = {o for o in pipes if rnd.random() < 0.8}
    traced, _ = NEW._trace_upstream_directed([seed_oid], net, ups, dns, infer_direction=True,
                                             log_fn=None, allowed=allowed)
    check(f"net {seed}: only allowed mains traced", set(traced) <= allowed, set(traced) - allowed)

# ---- 3. hand-built cases ----------------------------------------------------
A, B, C, D, E, F = [(i, 0) for i in range(6)]
# HFML 1 runs B->A.  Branch 1: C->B (oid 2), D->C (oid 3).  Branch 2: E->B (oid 4), F->E (oid 5)
pipes = {1: (B, A), 2: (C, B), 3: (D, C), 4: (E, B), 5: (F, E)}
net, ups, dns = network(pipes)
lines = []
traced, _ = NEW._trace_upstream_directed([1], net, ups, dns, log_fn=lines.append, allowed={1, 3, 4, 5})
check("blocked branch: neither the main nor anything above it", 2 not in traced and 3 not in traced, traced)
check("other branch still traced", sorted(traced) == [4, 5], traced)
check("stop is logged with the main", any("Stopped at 1 main(s)" in l and "2" in l for l in lines), lines)

traced, _ = NEW._trace_upstream_directed([1], net, ups, dns, log_fn=None, allowed={2, 3, 4, 5})
check("non-operating HFML still traced from", sorted(traced) == [2, 3, 4, 5], traced)

# a pipe digitized against the flow, dead-ending: followed when operating, not when not
pipes = {1: (B, A), 2: (C, B), 9: (C, D)}   # 9 starts at traced C, runs to dead end D
net, ups, dns = network(pipes)
traced_all, _ = NEW._trace_upstream_directed([1], net, ups, dns, log_fn=None, allowed=None)
traced_f, _ = NEW._trace_upstream_directed([1], net, ups, dns, log_fn=None, allowed={1, 2})
check("against-flow pipe followed normally", 9 in traced_all, traced_all)
check("against-flow pipe not followed when non-operating", 9 not in traced_f, traced_f)

# ---- 4. leftover rows are cleared, PMs layer untouched ---------------------
geom = S.Geom("point", S.Pt(0, 0))
data = {"Trace_SSGRAVITYMAIN": {189: {"SHAPE": geom}, 190: {"SHAPE": geom}},
        "Lower_Laterals": {712: {"SHAPE": geom}},
        "Parcels_trace": {},
        "PMs_SSGRAVITYMAIN": {5: {"SHAPE": geom}}}
a = S.Arcpy(data); lines = []
NEW._clear_leftover_trace_rows(a, ["Trace_SSGRAVITYMAIN", "Lower_Laterals", "Parcels_trace"], lines.append)
check("leftover mains removed", a.datasets["Trace_SSGRAVITYMAIN"] == {}, a.datasets["Trace_SSGRAVITYMAIN"])
check("leftover laterals removed", a.datasets["Lower_Laterals"] == {}, a.datasets["Lower_Laterals"])
check("HFML seeds untouched", list(a.datasets["PMs_SSGRAVITYMAIN"]) == [5], a.datasets["PMs_SSGRAVITYMAIN"])
check("removals logged per table",
      any("Removed 2 leftover" in l and "Trace_SSGRAVITYMAIN" in l and "189" in l for l in lines)
      and any("Removed 1 leftover" in l and "Lower_Laterals" in l for l in lines), lines)
check("empty table is silent", not any("Parcels_trace" in l for l in lines), lines)

# ---- 5. the main filter check uses main wording -----------------------------
mains = {1: {"SHAPE": geom, "STATUS": "OPERATING"}, 2: {"SHAPE": geom, "STATUS": "ABANDONED"}}
a = S.Arcpy({"ml": mains}); lines = []
got = NEW._check_status_filter(a, "ml", "STATUS = 'OPERATING'", lines.append, label="main",
                               config_key="p4_ml_status_filter")
check("main filter accepted", got == "STATUS = 'OPERATING'" and any("Main status filter" in l for l in lines), lines)
lines = []
NEW._check_status_filter(a, "ml", "STATUS = 'NOPE'", lines.append, label="main", config_key="p4_ml_status_filter")
check("main matches-nothing warning", any("matches no mains" in l for l in lines), lines)

print()
print("FAILURES: " + (", ".join(fails[:10]) if fails else "none"))
sys.exit(1 if fails else 0)

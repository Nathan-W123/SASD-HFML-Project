import importlib.util, math, random, sys
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
    return cond

from scenarios import line, rect, scenario

def run_old(data, traced, dn):
    a = S.Arcpy(data); logs = []
    lls = OLD._select_lls_contacting_mls(a, traced, "ml", "ll", downstream_point=dn, log_fn=logs.append)
    ps = OLD._select_parcels_intersecting_lls(a, lls, "ll", "parcels", ml_oid_list=traced,
                                             ml_source="ml", log_fn=logs.append)
    return lls, ps, logs, a

def run_new(data, traced, dn, pool=None, a=None):
    a = a or S.Arcpy(data); logs = []
    lls = NEW._select_lls_contacting_mls(a, traced, "ml", "ll", downstream_point=dn,
                                         log_fn=logs.append, layers=pool)
    ps = NEW._select_parcels_intersecting_lls(a, lls, "ll", "parcels", ml_oid_list=traced,
                                             ml_source="ml", log_fn=logs.append, layers=pool)
    return lls, ps, logs, a

def comparable(logs):
    # the old code's endpoint-hit line used GetCount, which reports the total
    # when nothing is selected; compare everything else verbatim
    return [l for l in logs if not l.startswith("LL endpoint hits")]

N = 300
ll_total = p_total = 0
for seed in range(N):
    data, traced, dn = scenario(seed)
    lo, po, logo, ao = run_old(data, traced, dn)
    ln, pn, logn, an = run_new(data, traced, dn)
    ll_total += len(lo); p_total += len(po)
    check(f"seed {seed}: same laterals", lo == ln, (lo, ln))
    check(f"seed {seed}: same parcels", po == pn, (po, pn))
    check(f"seed {seed}: same log lines", comparable(logo) == comparable(logn),
          (comparable(logo), comparable(logn)))
    check(f"seed {seed}: new code creates no feature classes",
          an.calls.get("CreateFeatureclass", 0) == 0 and an.calls.get("AddField", 0) == 0, an.calls)
print(f"{N} scenarios compared: {ll_total} laterals and {p_total} parcels selected by the old code")

# The same results through one shared pool, HFML after HFML, as the run does.
a = None
pool = None
for seed in range(40):
    data, traced, dn = scenario(seed)
    lo, po, _, _ = run_old(data, traced, dn)
    if a is None or True:
        # a fresh stand-in per scenario (different data), but one pool across
        # two HFMLs on the same data to prove no selection leaks between them
        a = S.Arcpy(data); pool = NEW._LayerPool(a)
    ln1, pn1, _, _ = run_new(data, traced, dn, pool=pool, a=a)
    ln2, pn2, _, _ = run_new(data, traced[:1], dn, pool=pool, a=a)
    lo2, po2, _, _ = run_old(data, traced[:1], dn)
    check(f"pool seed {seed}: first HFML matches", (ln1, pn1) == (lo, po))
    check(f"pool seed {seed}: second HFML on same pool matches", (ln2, pn2) == (lo2, po2),
          ((ln2, pn2), (lo2, po2)))
    pool.close()
    check(f"pool seed {seed}: close removes the layers", not any(k.startswith("_HFMLPool_") for k in a.layers),
          list(a.layers))

# Tool-call budget per HFML, old vs new, on a typical scenario
data, traced, dn = scenario(7)
_, _, _, ao = run_old(data, traced, dn)
a = S.Arcpy(data); pool = NEW._LayerPool(a)
run_new(data, traced, dn, pool=pool, a=a)
slow = ("CreateFeatureclass", "AddField", "Delete")
old_slow = sum(ao.calls.get(k, 0) for k in slow)
new_slow = sum(a.calls.get(k, 0) for k in slow)
print(f"schema-changing tool calls per HFML: old {old_slow} "
      f"({', '.join(f'{k} {ao.calls.get(k,0)}' for k in slow)}), "
      f"new {new_slow} ({', '.join(f'{k} {a.calls.get(k,0)}' for k in slow)})")
check("new makes far fewer schema-changing calls", new_slow <= 2 and old_slow >= 10, (old_slow, new_slow))

# No laterals near the trace at all
data, traced, dn = scenario(11, with_laterals=False)
data["ll"] = {1: {"SHAPE": line((5000, 5000), (5040, 5000))},
              2: {"SHAPE": line((6000, 6000), (6040, 6000))}}
data["parcels"] = {1: {"SHAPE": rect(5030, 4990, 5060, 5010)}}
ln, pn, logs, _ = run_new(data, traced, dn)
check("no nearby laterals -> none selected", ln == [] and pn == [], (ln, pn, logs))
lo, po, _, ao = run_old(data, traced, dn)
# The old code was slow here, not wrong: its empty selection made the candidate
# layer every lateral, but the empty selection after the endpoint test then
# returned nothing through the cursor.
check("old code also selects none", lo == [] and po == [], (lo, po))

print()
print("FAILURES: " + (", ".join(fails[:10]) + (" ..." if len(fails) > 10 else "") if fails else "none"))
sys.exit(1 if fails else 0)

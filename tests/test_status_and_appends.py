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
import scenarios as EQ

fails = []
def check(name, cond, detail=""):
    if not cond:
        print("FAIL " + name + " :: " + str(detail)); fails.append(name)

FILTER = "STATUS IN ('OPERATING', 'TOBEMOD')"
STATUSES = ["OPERATING"] * 7 + ["TOBEMOD", "ABANDONED", "PROPOSED"]

def with_statuses(seed):
    data, traced, dn = EQ.scenario(seed)
    rnd = random.Random(seed + 10_000)
    for row in data["ll"].values():
        row["STATUS"] = rnd.choice(STATUSES)
    return data, traced, dn

# ---- 1. status filter: exactly the old selection minus non-operating ones --
dropped_total = kept_total = 0
for seed in range(300):
    data, traced, dn = with_statuses(seed)
    status = {o: r["STATUS"] for o, r in data["ll"].items()}

    a_old = S.Arcpy(data)
    old_lls = OLD._select_lls_contacting_mls(a_old, traced, "ml", "ll", downstream_point=dn, log_fn=lambda m: None)
    expected_lls = [o for o in old_lls if status[o] in ("OPERATING", "TOBEMOD")]
    expected_parcels = OLD._select_parcels_intersecting_lls(
        S.Arcpy(data), expected_lls, "ll", "parcels", ml_oid_list=traced, ml_source="ml", log_fn=lambda m: None)

    a_new = S.Arcpy(data); pool = NEW._LayerPool(a_new); logs = []
    new_lls = NEW._select_lls_contacting_mls(a_new, traced, "ml", "ll", downstream_point=dn,
                                             log_fn=logs.append, layers=pool, status_filter=FILTER)
    new_parcels = NEW._select_parcels_intersecting_lls(a_new, new_lls, "ll", "parcels", ml_oid_list=traced,
                                                      ml_source="ml", log_fn=logs.append, layers=pool)
    check(f"seed {seed}: filtered laterals", new_lls == expected_lls, (new_lls, expected_lls))
    check(f"seed {seed}: parcels follow the laterals", new_parcels == expected_parcels,
          (new_parcels, expected_parcels))
    dropped_total += len(old_lls) - len(expected_lls); kept_total += len(expected_lls)
print(f"status filter: kept {kept_total} laterals, dropped {dropped_total} non-operating ones across 300 scenarios")

# filter off still matches the old code exactly
for seed in range(50):
    data, traced, dn = with_statuses(seed)
    old_lls = OLD._select_lls_contacting_mls(S.Arcpy(data), traced, "ml", "ll", downstream_point=dn, log_fn=lambda m: None)
    a = S.Arcpy(data)
    new_lls = NEW._select_lls_contacting_mls(a, traced, "ml", "ll", downstream_point=dn,
                                             log_fn=lambda m: None, layers=NEW._LayerPool(a), status_filter=None)
    check(f"seed {seed}: filter off matches old", new_lls == old_lls, (new_lls, old_lls))

# ---- 2. the startup check --------------------------------------------------
data, traced, dn = with_statuses(1)
a = S.Arcpy(data); lines = []
check("valid filter kept", NEW._check_status_filter(a, "ll", FILTER, lines.append) == FILTER, lines)
lines.clear()
check("bad field switched off", NEW._check_status_filter(a, "ll", "STATUZ IN ('X') AND", lines.append) is None, lines)
check("bad field warns", any("not valid" in l for l in lines), lines)
lines.clear()
check("matches-nothing switched off",
      NEW._check_status_filter(a, "ll", "STATUS IN ('NO_SUCH_STATUS')", lines.append) is None, lines)
check("matches-nothing warns", any("matches no laterals" in l for l in lines), lines)
lines.clear()
check("empty means off", NEW._check_status_filter(a, "ll", "  ", lines.append) is None and
      any("off" in l for l in lines), lines)

# ---- 3. direct row copy == Append NO_TEST ----------------------------------
F = S.Field
def append_data(seed):
    rnd = random.Random(seed)
    src = {}
    for oid in range(1, 40):
        x, y = rnd.uniform(0, 1000), rnd.uniform(0, 1000)
        src[oid * 3] = {"SHAPE": S.Geom("line", [S.Pt(x, y), S.Pt(x + 30, y + 10)]),
                        "STATUS": rnd.choice(STATUSES), "DIAMETER": rnd.choice([4.0, 6.0, 8.0]),
                        "MATERIAL": rnd.choice(["VCP", "PVC", None]), "SOURCE_ONLY": "x"}
    schemas = {
        "src": [F("OBJECTID", "OID", False), F("Shape", "Geometry"), F("STATUS"), F("DIAMETER", "Double"),
                F("MATERIAL"), F("SOURCE_ONLY")],
        # destination: different case, a field the source lacks, a read-only field
        "dst": [F("OBJECTID", "OID", False), F("Shape", "Geometry"), F("status"), F("Diameter", "Double"),
                F("MATERIAL"), F("DEST_ONLY"), F("Shape_Length", "Double", editable=False)],
    }
    return {"src": src, "dst": {}}, schemas

def contents(a):
    rows = []
    for oid, r in a.datasets["dst"].items():
        g = r["SHAPE"]
        rows.append((g.firstPoint.X, g.firstPoint.Y,
                     # a field left unset and a field set to null are both NULL in a geodatabase
                     tuple(sorted((k.lower(), v) for k, v in r.items() if k != "SHAPE" and v is not None))))
    return sorted(rows)

for seed in range(30):
    data, schemas = append_data(seed)
    picks = sorted(random.Random(seed).sample(sorted(data["src"]), 15))
    a_old = S.Arcpy(data, schemas); OLD._append_by_oids(a_old, picks, "src", "dst")
    NEW._APPEND_PLANS.clear()
    a_new = S.Arcpy(data, schemas); NEW._append_by_oids(a_new, picks, "src", "dst")
    check(f"append seed {seed}: same rows as Append", contents(a_new) == contents(a_old),
          (contents(a_new)[:2], contents(a_old)[:2]))
    check(f"append seed {seed}: no Append tool call", a_new.calls.get("Append", 0) == 0, a_new.calls)

# fails part-way: partial rows removed, then Append inserts each row once
data, schemas = append_data(99)
picks = sorted(data["src"])[:12]
NEW._APPEND_PLANS.clear()
a = S.Arcpy(data, schemas)
real_insert = a._insert
def flaky_insert(path, fields):
    ins = real_insert(path, fields); count = {"n": 0}; real_row = ins.insertRow
    def insertRow(values):
        count["n"] += 1
        if count["n"] == 6: raise RuntimeError("Objects in this class cannot be updated outside an edit session")
        return real_row(values)
    ins.insertRow = insertRow
    return ins
a.da.InsertCursor = flaky_insert
lines = []
NEW._append_by_oids(a, picks, "src", "dst", log_fn=lines.append)
check("partial copy cleaned up, then Append: each row once", len(a.datasets["dst"]) == 12, len(a.datasets["dst"]))
check("fell back to Append", a.calls.get("Append", 0) == 1, a.calls)
check("fallback reported", any("direct row copy" in l and "edit session" in l for l in lines), lines)
NEW._append_by_oids(a, picks[:2], "src", "dst", log_fn=lines.append)
check("fast path not retried after it failed", a.calls.get("Append", 0) == 2, a.calls)

# different coordinate systems: never use the direct copy
data, schemas = append_data(5)
NEW._APPEND_PLANS.clear()
a = S.Arcpy(data, schemas, srs={"src": (2226, "StatePlane II"), "dst": (4326, "WGS 84")})
NEW._append_by_oids(a, sorted(data["src"])[:5], "src", "dst")
check("different coordinate systems use Append", a.calls.get("Append", 0) == 1, a.calls)

print()
print("FAILURES: " + (", ".join(fails[:10]) + (" ..." if len(fails) > 10 else "") if fails else "none"))
sys.exit(1 if fails else 0)

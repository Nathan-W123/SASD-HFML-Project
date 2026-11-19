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
check("  and the next call will try the direct copy again", any("tried again next time" in l for l in lines), lines)
NEW._append_by_oids(a, picks[:2], "src", "dst", log_fn=lines.append)
check("direct copy tried again on the next call", a.calls.get("Append", 0) == 1 and len(a.datasets["dst"]) == 14,
      (a.calls, len(a.datasets["dst"])))

NEW.DIRECT_COPY_LOCK_WAIT = 0       # no real waiting in tests
def reference(picks, seed):
    data, schemas = append_data(seed)
    a_ref = S.Arcpy(data, schemas); OLD._append_by_oids(a_ref, picks, "src", "dst")
    return contents(a_ref)

# 2026-11-19: the insert cursor hit "Cannot acquire a lock" when it closed, so
# nothing was committed, and removing the rows hit the same lock.
data, schemas = append_data(7)
picks = sorted(data["src"])[:9]
NEW._APPEND_PLANS.clear()
a = S.Arcpy(data, schemas)
real_insert, real_update = a._insert, a._update
state = {"insert_locks": 1, "update_locks": 1}
def locked_on_close(path, fields):
    ins = real_insert(path, fields)
    if not state["insert_locks"]:
        return ins
    state["insert_locks"] -= 1
    added = []; real_row = ins.insertRow
    ins.insertRow = lambda values: added.append(real_row(values)) or added[-1]
    def exit_(*exc):
        for oid in added: a.datasets[path].pop(oid, None)      # never committed
        raise RuntimeError("Cannot acquire a lock.")
    ins.__exit__ = exit_
    return ins
class _LockedOnce:
    def __init__(self, inner): self.inner = inner
    def __enter__(self):
        if state["update_locks"]:
            state["update_locks"] -= 1
            raise RuntimeError("Cannot acquire a lock.")
        return self.inner.__enter__()
    def __exit__(self, *e): return self.inner.__exit__(*e)
a.da.UpdateCursor = lambda path, fields, where=None: _LockedOnce(real_update(path, fields, where))
class _ExitHook:   # `with` looks __exit__ up on the class, so wrap the stub cursor
    def __init__(self, ins): self.ins = ins
    def __enter__(self): return self.ins
    def __exit__(self, *e): return self.ins.__exit__(*e)
a.da.InsertCursor = lambda path, fields: _ExitHook(locked_on_close(path, fields))
lines = []
NEW._append_by_oids(a, picks, "src", "dst", log_fn=lines.append)
check("lock on commit: rows copied once", contents(a) == reference(picks, 7), (len(a.datasets["dst"]), len(picks)))
check("lock on commit: no Append tool call", a.calls.get("Append", 0) == 0, a.calls)
check("lock on commit: retry reported", any("was locked" in l for l in lines), lines)
check("lock on commit: fast path kept", NEW._APPEND_PLANS[("src", "dst")]["direct"]
      and NEW._APPEND_PLANS[("src", "dst")]["failures"] == 0, NEW._APPEND_PLANS)

# fails part-way and the rows it inserted cannot be removed: Append only the rest
def failing_insert(a, at, message):
    real = a._insert
    def make(path, fields):
        ins = real(path, fields); n = {"n": 0}; real_row = ins.insertRow
        def insertRow(values):
            n["n"] += 1
            if n["n"] == at: raise RuntimeError(message)
            return real_row(values)
        ins.insertRow = insertRow
        return ins
    return make
class _NoDelete:
    def __init__(self, inner): self.inner = inner
    def __enter__(self): self.inner.__enter__(); return self
    def __exit__(self, *e): return False
    def __iter__(self): return iter(self.inner)
    def deleteRow(self): raise RuntimeError("Cannot acquire a lock.")
data, schemas = append_data(8)
picks = sorted(data["src"])[:12]
NEW._APPEND_PLANS.clear()
a = S.Arcpy(data, schemas)
a.da.InsertCursor = failing_insert(a, 6, "The row contains a bad value")
upd = a._update
a.da.UpdateCursor = lambda path, fields, where=None: _NoDelete(upd(path, fields, where))
lines = []
NEW._append_by_oids(a, picks, "src", "dst", log_fn=lines.append)
check("rows left behind: each row once", contents(a) == reference(picks, 8), (len(a.datasets["dst"]), len(picks)))
check("rows left behind: Append only for the rest", a.calls.get("Append", 0) == 1, a.calls)

# ...and when even reading the destination fails, stop rather than risk duplicates
data, schemas = append_data(9)
picks = sorted(data["src"])[:12]
NEW._APPEND_PLANS.clear()
a = S.Arcpy(data, schemas)
a.da.InsertCursor = failing_insert(a, 6, "The row contains a bad value")
upd, search = a._update, a._search
a.da.UpdateCursor = lambda path, fields, where=None: _NoDelete(upd(path, fields, where))
def unreadable(path, fields, where=None):
    if path == "dst": raise RuntimeError("Cannot acquire a lock.")
    return search(path, fields, where)
a.da.SearchCursor = unreadable
try:
    NEW._append_by_oids(a, picks, "src", "dst")
    raised = False
except NEW._DirectCopyUnknown:
    raised = True
check("unknown leftovers: the HFML fails", raised)
check("unknown leftovers: no Append on top", a.calls.get("Append", 0) == 0, a.calls)

# two failures in a row: Append for the rest of the run
data, schemas = append_data(10)
picks = sorted(data["src"])[:4]
NEW._APPEND_PLANS.clear()
a = S.Arcpy(data, schemas)
tries = {"n": 0}
def always_fails(path, fields):
    tries["n"] += 1
    raise RuntimeError("Objects in this class cannot be updated outside an edit session")
a.da.InsertCursor = always_fails
lines = []
for _ in range(3):
    NEW._append_by_oids(a, picks, "src", "dst", log_fn=lines.append)
check("two failures in a row: direct copy given up", tries["n"] == 2 and a.calls.get("Append", 0) == 3,
      (tries, a.calls))
check("  and reported", any("rest of this run" in l for l in lines), lines)

# different coordinate systems: never use the direct copy
data, schemas = append_data(5)
NEW._APPEND_PLANS.clear()
a = S.Arcpy(data, schemas, srs={"src": (2226, "StatePlane II"), "dst": (4326, "WGS 84")})
NEW._append_by_oids(a, sorted(data["src"])[:5], "src", "dst")
check("different coordinate systems use Append", a.calls.get("Append", 0) == 1, a.calls)

print()
print("FAILURES: " + (", ".join(fails[:10]) + (" ..." if len(fails) > 10 else "") if fails else "none"))
sys.exit(1 if fails else 0)

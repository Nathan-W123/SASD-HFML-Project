import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
import shutil, tempfile, time, types
from backend.phase4.network_cache import dataset_signature

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond: fails.append(name)

d = tempfile.mkdtemp()
gdb = os.path.join(d, "SQL_WEB.gdb")
os.makedirs(gdb)

def write(name, size, mtime=None):
    path = os.path.join(gdb, name)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    if mtime:
        os.utime(path, (mtime, mtime))

base = time.time() - 10_000
write("a00000001.gdbtable", 4_000, base)          # the catalog
write("a00000010.gdbtable", 300_000, base)        # ours: DSID 16, 1,000 rows
write("a00000010.gdbtablx", 5_000, base)
write("a00000010.spx", 9_000, base)
write("a00000011.gdbtable", 800_000, base)        # another layer in the same gdb
write("a00000012.gdbtable", 200, base)            # a tiny lookup table

def fake(dsid=16, count=1000):
    a = types.SimpleNamespace()
    a.Describe = lambda p: types.SimpleNamespace(DSID=dsid) if dsid is not None else types.SimpleNamespace()
    a.management = types.SimpleNamespace(GetCount=lambda p: [str(count)])
    return a

FC = os.path.join(gdb, "SASD_SSGRAVITYMAIN")
sig = dataset_signature(fake(), FC)
check("our table's files only", sig["scope"] == "table" and sig["total_size"] == 314_000, sig)

write("a00000011.gdbtable", 900_000, base + 5_000)      # another layer republished
check("another layer changing leaves it alone", dataset_signature(fake(), FC) == sig,
      dataset_signature(fake(), FC))
write("a00000001.gdbtable", 4_100, base + 6_000)        # catalog touched
check("catalog changing leaves it alone", dataset_signature(fake(), FC) == sig)

write("a00000010.gdbtable", 300_000, base + 7_000)      # ours edited in place
check("our table changing is caught", dataset_signature(fake(), FC) != sig)
sig2 = dataset_signature(fake(), FC)
write("a00000010.spx", 9_500, base + 7_000)             # our spatial index rebuilt
check("our index files count too", dataset_signature(fake(), FC) != sig2)
check("row count change is caught",
      dataset_signature(fake(count=999), FC) != dataset_signature(fake(count=1000), FC))

# every doubt falls back to the whole geodatabase
check("no DSID: whole geodatabase", dataset_signature(fake(dsid=None), FC)["scope"] == "geodatabase")
check("DSID with no table file: whole geodatabase", dataset_signature(fake(dsid=99), FC)["scope"] == "geodatabase")
check("DSID pointing at a tiny other table: whole geodatabase",
      dataset_signature(fake(dsid=18), FC)["scope"] == "geodatabase")
check("no row count: whole geodatabase",
      dataset_signature(types.SimpleNamespace(Describe=fake().Describe,
                        management=types.SimpleNamespace(GetCount=lambda p: 1 / 0)), FC)["scope"] == "geodatabase")
whole = dataset_signature(fake(dsid=None), FC)
write("a00000011.gdbtable", 950_000, base + 9_000)
check("whole-geodatabase scope still sees other layers change (as before)",
      dataset_signature(fake(dsid=None), FC) != whole)

shutil.rmtree(d, ignore_errors=True)
print()
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)

import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
import shutil, struct, tempfile, time, types
import backend.phase4.network_cache as nc
from backend.phase4.network_cache import dataset_signature

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond: fails.append(name)

d = tempfile.mkdtemp()
gdb = os.path.join(d, "SQL_WEB.gdb")
os.makedirs(gdb)
base = time.time() - 10_000

def touch(path, mtime):
    os.utime(path, (mtime, mtime))

def table(table_id, rows, fields, geometry, size=1_000, mtime=base, version=3, ext="gdbtable"):
    """A .gdbtable laid out the way GDAL's OpenFileGDB reader expects:
    version, row count (int32 at 4 in v3, int64 at 16 in v4), field section
    offset at 32; in the field section, geometry type at 8, field count at 12."""
    head = bytearray(40)
    struct.pack_into("<i", head, 0, version)
    if version == 3:
        struct.pack_into("<i", head, 4, rows)
    else:
        struct.pack_into("<q", head, 16, rows)
    struct.pack_into("<Q", head, 32, 40)
    section = bytearray(14)
    struct.pack_into("<I", section, 0, 200)
    struct.pack_into("<I", section, 4, 4)
    section[8] = geometry
    struct.pack_into("<H", section, 12, fields)
    body = bytes(head) + bytes(section)
    path = os.path.join(gdb, f"{table_id}.{ext}")
    with open(path, "wb") as f:
        f.write(body + b"x" * max(0, size - len(body)))
    touch(path, mtime)
    return path

def other(name, size, mtime=base):
    path = os.path.join(gdb, name)
    with open(path, "wb") as f:
        f.write(b"x" * size)
    touch(path, mtime)
    return path

LINE, POLY = 3, 4
table("a00000001", 13, 3, 0, size=4_000)                 # the catalog
ours = table("a00000010", 1000, 12, LINE, size=300_000)  # the mains: 1,000 rows, 12 fields
other("a00000010.gdbtablx", 5_000)
spx = other("a00000010.spx", 9_000)
table("a00000011", 2000, 12, LINE, size=800_000)         # another layer in the same gdb
table("a00000012", 1000, 9, LINE, size=200)              # same row count, other fields
table("a00000013", 1000, 12, POLY, size=600_000)         # same counts, polygons

def fake(dsid=None, count=1000, fields=12, shape="Polyline"):
    a = types.SimpleNamespace()
    attrs = {"shapeType": shape}
    if dsid is not None:
        attrs["DSID"] = dsid
    a.Describe = lambda p: types.SimpleNamespace(**attrs)
    a.ListFields = lambda p: [object()] * fields
    a.management = types.SimpleNamespace(GetCount=lambda p: [str(count)])
    return a

FC = os.path.join(gdb, "SASD_SSGRAVITYMAIN")
notes = []
sig = dataset_signature(fake(), FC, notes=notes)
check("found by its header with no DSID", sig["scope"] == "table" and sig["table"] == "a00000010", sig)
check("only our table's files are counted", sig["total_size"] == 314_000, sig)
check("same counts but polygons is not taken for it", sig["table"] != "a00000013")

table("a00000011", 2100, 12, LINE, size=900_000, mtime=base + 5_000)   # another layer republished
check("another layer changing leaves it alone", dataset_signature(fake(), FC) == sig,
      dataset_signature(fake(), FC))
table("a00000001", 14, 3, 0, size=4_100, mtime=base + 6_000)            # catalog touched
check("catalog changing leaves it alone", dataset_signature(fake(), FC) == sig)

table("a00000010", 1000, 12, LINE, size=300_000, mtime=base + 7_000)    # ours edited in place
check("our table changing is caught", dataset_signature(fake(), FC) != sig)
sig2 = dataset_signature(fake(), FC)
other("a00000010.spx", 9_500, base + 7_000)                              # our spatial index rebuilt
check("our index files count too", dataset_signature(fake(), FC) != sig2)
table("a00000010", 999, 12, LINE, size=300_000, mtime=base + 7_000)
check("row count change is caught",
      dataset_signature(fake(count=999), FC) != dataset_signature(fake(count=1000), FC))
table("a00000010", 1000, 12, LINE, size=300_000, mtime=base + 7_000)

check("right DSID is used", dataset_signature(fake(dsid=16), FC)["table"] == "a00000010")
notes = []
wrong = dataset_signature(fake(dsid=17), FC, notes=notes)
check("wrong DSID is checked and not trusted", wrong["table"] == "a00000010", wrong)
check("  and the mismatch is noted", any("DSID 17 names a00000011" in n for n in notes), notes)

# The table ID stored last run is tried first, so the geodatabase is not scanned.
reads = []
real_read = nc._read_table_header
nc._read_table_header = lambda p: reads.append(p) or real_read(p)
again = dataset_signature(fake(), FC, previous=sig2)
nc._read_table_header = real_read
check("last run's table ID needs one header read", len(reads) == 1 and again["table"] == "a00000010",
      (len(reads), again))
stale_hint = dict(sig2, table="a00000011")      # that ID now holds some other layer
check("last run's ID is checked, not trusted",
      dataset_signature(fake(), FC, previous=stale_hint)["table"] == "a00000010")

# The table recreated under a new ID: last run's hint fails, the scan finds it.
for name in os.listdir(gdb):
    if name.startswith("a00000010."):
        os.rename(os.path.join(gdb, name), os.path.join(gdb, name.replace("a00000010", "a00000020")))
moved = dataset_signature(fake(), FC, previous=sig2)
check("a recreated table is found under its new ID", moved["table"] == "a00000020", moved)
check("  and counts as a change", moved != sig2)

# Version 4 (ArcGIS Pro 3.2+, 64-bit object IDs) keeps the row count elsewhere.
table("a00000020", 1000, 12, LINE, size=300_000, version=4)
check("version 4 header is read", dataset_signature(fake(), FC)["table"] == "a00000020")
table("a00000020", 1000, 12, LINE, size=300_000)

# Every doubt falls back to the whole geodatabase.
table("a00000021", 1000, 12, LINE, size=300_000)                        # an exact twin
notes = []
twin = dataset_signature(fake(), FC, notes=notes)
check("two matching tables: whole geodatabase", twin["scope"] == "geodatabase" and twin["table"] is None, twin)
check("  and says why", any("2 tables have 1000 rows, 12 fields and polyline geometry" in n for n in notes), notes)
os.remove(os.path.join(gdb, "a00000021.gdbtable"))
notes = []
check("no table with our field count: whole geodatabase",
      dataset_signature(fake(fields=13), FC, notes=notes)["scope"] == "geodatabase")
check("  and says why", any("none of the geodatabase's" in n for n in notes), notes)
check("unknown shape type: whole geodatabase",
      dataset_signature(fake(shape="Annotation"), FC)["scope"] == "geodatabase")
check("no row count: whole geodatabase",
      dataset_signature(types.SimpleNamespace(Describe=fake().Describe, ListFields=fake().ListFields,
                        management=types.SimpleNamespace(GetCount=lambda p: 1 / 0)), FC)["scope"] == "geodatabase")
whole = dataset_signature(fake(fields=13), FC)
table("a00000011", 2200, 12, LINE, size=950_000, mtime=base + 9_000)
check("whole-geodatabase scope still sees other layers change (as before)",
      dataset_signature(fake(fields=13), FC) != whole)

shutil.rmtree(d, ignore_errors=True)
print()
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)

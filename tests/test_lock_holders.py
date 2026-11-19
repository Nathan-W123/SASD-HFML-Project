"""A locked destination is retried briefly, then reported with who holds it."""
import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
import platform, shutil, struct, tempfile, types
import backend.phase4.run as P4

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond: fails.append(name)

d = tempfile.mkdtemp()
gdb = os.path.join(d, "Data.gdb")
os.makedirs(gdb)

def table(table_id, rows, fields, geometry):
    head = bytearray(40)
    struct.pack_into("<i", head, 0, 3)
    struct.pack_into("<i", head, 4, rows)
    struct.pack_into("<Q", head, 32, 40)
    section = bytearray(14)
    section[8] = geometry
    struct.pack_into("<H", section, 12, fields)
    with open(os.path.join(gdb, f"{table_id}.gdbtable"), "wb") as f:
        f.write(bytes(head) + bytes(section))

def lock(name):
    open(os.path.join(gdb, name), "w").close()

me = platform.node().split(".")[0]
table("a00000009", 6, 14, 3)           # Trace_SSGRAVITYMAIN
table("a0000000a", 45, 41, 3)          # Lower_Laterals
lock(f"a00000009.{me}.{os.getpid()}.1.sr.lock")     # this ArcGIS Pro itself
lock(f"a00000009.{me}.4242.7.sr.lock")              # a leftover export on this computer
lock("a00000009.OTHERPC.5555.3.ed.lock")            # someone else editing it
lock("a0000000a.OTHERPC.6666.1.sr.lock")            # another table
lock("_gdb.OTHERPC.7777.1.sr.lock")                 # just has the geodatabase open

TRACE = os.path.join(gdb, "Trace_SSGRAVITYMAIN")

def fake(free_after=None, rows=6, fields=14):
    asked = {"n": 0}
    def test_lock(path):
        asked["n"] += 1
        return free_after is not None and asked["n"] > free_after
    a = types.SimpleNamespace(
        TestSchemaLock=test_lock,
        ListFields=lambda p: [object()] * fields,
        Describe=lambda p: types.SimpleNamespace(shapeType="Polyline"),
        management=types.SimpleNamespace(GetCount=lambda p: [str(rows)]),
    )
    return a, asked

real_sleep = P4.time.sleep
slept = []
P4.time.sleep = slept.append
try:
    a, asked = fake()
    lines = []
    try:
        P4._require_write_lock(a, TRACE, "ML destination", lines.append)
        message = None
    except RuntimeError as exc:
        message = str(exc)
    check("still locked: fails", message is not None)
    check("  after asking three times, 2s and 5s apart", asked["n"] == 3 and slept == [2, 5], (asked, slept))
    check("  names the process on this computer", "process 4242 on this computer (reading)" in message, message)
    check("  names the other computer", "computer OTHERPC, process 5555 (editing)" in message, message)
    check("  leaves out this ArcGIS Pro", f"process {os.getpid()} " not in message, message)
    check("  leaves out other tables and plain geodatabase use",
          "6666" not in message and "7777" not in message, message)
    check("  keeps the advice", "then rerun Phase 4" in message, message)

    slept.clear()
    a, asked = fake(free_after=1)
    lines = []
    P4._require_write_lock(a, TRACE, "ML destination", lines.append)
    check("freed while waiting: carries on", asked["n"] == 2 and slept == [2], (asked, slept))
    check("  and says it waited", any("in use — checking again in 2s" in l for l in lines), lines)

    slept.clear()
    a, asked = fake(free_after=0)
    P4._require_write_lock(a, TRACE, "ML destination", lines.append)
    check("free at once: no wait", slept == [])

    # the table's files cannot be identified: every lock in the geodatabase
    a, _ = fake(rows=999)
    try:
        P4._require_write_lock(a, TRACE, "ML destination", waits=())
        message = None
    except RuntimeError as exc:
        message = str(exc)
    check("table unknown: lists every holder in the geodatabase",
          message and all(p in message for p in ("4242", "5555", "6666", "7777")), message)

    # no lock files to read: the message is as before
    a, _ = fake()
    try:
        P4._require_write_lock(a, os.path.join(d, "elsewhere", "x"), "ML destination", waits=())
        message = None
    except RuntimeError as exc:
        message = str(exc)
    check("no lock files: plain message", message and "Held by" not in message, message)
finally:
    P4.time.sleep = real_sleep
    shutil.rmtree(d, ignore_errors=True)

print()
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)

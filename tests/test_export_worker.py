import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
import re, shutil, tempfile, types
from unittest import mock
sys.modules.setdefault("pyodbc", types.ModuleType("pyodbc"))
import backend.phase5.run as P5

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond: fails.append(name)

FAKE = [sys.executable, "-u", os.path.join(os.path.dirname(os.path.abspath(__file__)), "export_worker_fake.py")]
d = tempfile.mkdtemp()
def job(n, **extra):
    j = {"pmnum": str(n), "pdf_path": os.path.join(d, f"{n}.pdf")}
    j.update(extra)
    return j
def scratch(n):
    path = os.path.join(d, f"_phase5_{n}_tmp.aprx")
    open(path, "w").close()
    return path
def pids(lines):
    return [int(m.group(1)) for l in lines for m in [re.match(r"pid (\d+) exporting", l)] if m]

try:
    w = P5._ExportWorker(command=FAKE, max_maps=3)
    lines = []

    # three maps, one process
    for n in (1, 2, 3):
        s = scratch(n)
        w.export(job(n), s, lines.append)
        check(f"map {n}: scratch project removed", not os.path.exists(s))
    check("three maps exported", all(os.path.exists(job(n)["pdf_path"]) for n in (1, 2, 3)))
    check("one process served all three", len(set(pids(lines))) == 1, pids(lines))
    check("result markers never reach the log", not any(P5._RESULT_MARKER.strip() in l for l in lines), lines)
    first_pid = pids(lines)[0]

    # recycled after max_maps
    lines.clear()
    w.export(job(4), scratch(4), lines.append)
    check("fresh process after the limit", pids(lines) and pids(lines)[0] != first_pid, pids(lines))
    check("recycle logged", any("starting a fresh one after 3 maps" in l for l in lines), lines)
    second_pid = pids(lines)[0]

    # a map that fails is reported, and the process carries on
    lines.clear()
    raised = None
    try:
        w.export(job(5, fake="fail"), scratch(5), lines.append)
    except RuntimeError as exc:
        raised = str(exc)
    check("failed map raises", raised == "Map export for HFML 5 failed", raised)
    check("failure traceback logged", any("Layout 'Vertical Layout' not found" in l for l in lines), lines)
    lines.clear()
    w.export(job(6), scratch(6), lines.append)
    check("process survives a failed map", pids(lines) == [second_pid], (pids(lines), second_pid))

    # a crash mid-map: fresh process, one retry
    lines.clear()
    flag = os.path.join(d, "crashed_once")
    w.export(job(7, fake="crash_once", flag=flag), scratch(7), lines.append)
    check("crash retried once and succeeded", os.path.exists(job(7)["pdf_path"]))
    check("crash logged", any("export process stopped while exporting HFML 7" in l for l in lines), lines)
    check("retry ran in a new process", len(set(pids(lines))) == 2, pids(lines))

    # crashing every time: give up after the retry
    lines.clear()
    raised = None
    try:
        w.export(job(8, fake="crash"), scratch(8), lines.append)
    except RuntimeError as exc:
        raised = str(exc)
    check("repeated crash raises", raised and "stopped twice" in raised, raised)
    check("next map still works after giving up", w.export(job(9), scratch(9), lines.append) == job(9)["pdf_path"])

    # a process killed while keeping a project copy open: its copy is removed
    # once it has stopped; another process's copy in the same folder is not
    others = P5._open_copy_path(os.path.join(d, "TEST.aprx"), 1, pid=999999)
    open(others, "w").close()
    lines.clear()
    raised = None
    try:
        w.export(job(11, fake="keep_copy_and_crash"), scratch(11), lines.append)
    except RuntimeError as exc:
        raised = str(exc)
    killed = pids(lines)
    left = [n for n in os.listdir(d) if n.startswith("_phase5_open_") and n != os.path.basename(others)]
    check("killed process's open copy removed", raised and killed and left == [], (raised, killed, left))
    check("another process's copy left alone", os.path.exists(others))
    w.export(job(12), scratch(12), lines.append)       # and the next map gets a new process

    # a scratch project still locked is removed when the process stops
    locked = scratch(10)
    real_remove = os.remove
    with mock.patch("os.remove", side_effect=OSError("in use")):
        w._remove_scratch(locked, lines.append)
    check("locked scratch kept for later", locked in w.pending_scratch and os.path.exists(locked))
    proc = w.proc
    w.close(lines.append)
    check("close stops the process", proc.poll() is not None)
    check("close removes the pending scratch", not os.path.exists(locked) and w.pending_scratch == [])

    # ---- the switch: shared process by default, own process when off --------
    calls = []
    class FakeWorker:
        def __init__(self): calls.append("worker-started")
        def export(self, hfml, scratch, log_fn): calls.append(("shared", hfml["pmnum"])); return hfml["pdf_path"]
        def close(self, log_fn=None): calls.append("worker-closed")
    with mock.patch.object(P5, "_ExportWorker", FakeWorker), \
         mock.patch.object(P5, "_export_in_own_process",
                           lambda h, s, l: calls.append(("own", h["pmnum"])) or h["pdf_path"]):
        P5.close_export_worker()
        for n in (1, 2):
            P5.export_hfml_map_out_of_process(job(n, aprx_path=os.path.join(d, "TEST.aprx")), {}, lambda m: None)
        P5.export_hfml_map_out_of_process(job(3, aprx_path=os.path.join(d, "TEST.aprx")),
                                          {"p4_export_reuse_process": False}, lambda m: None)
        P5.close_export_worker()
    check("default shares one process", calls[:3] == ["worker-started", ("shared", "1"), ("shared", "2")], calls)
    check("switch off uses a process per map", ("own", "3") in calls, calls)
    check("closed at the end", calls[-1] == "worker-closed", calls)

    # ---- one map's source overrides never reach the next ---------------------
    base = {"p4_ml_source_path": "remote", "other": 1}
    c1 = P5._job_config(base, {"source_overrides": {"p4_ml_source_path": "local"}}, lambda m: None)
    c2 = P5._job_config(base, {}, lambda m: None)
    check("override applied to its own map", c1["p4_ml_source_path"] == "local")
    check("base config untouched", base["p4_ml_source_path"] == "remote")
    check("next map without overrides sees the original", c2["p4_ml_source_path"] == "remote")
finally:
    shutil.rmtree(d, ignore_errors=True)

print()
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)

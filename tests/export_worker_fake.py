"""Stand-in export process for test_export_worker: the real serving loop
(_export_worker_serve) with fake maps instead of ArcGIS."""
import os, sys, types
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
sys.modules.setdefault("pyodbc", types.ModuleType("pyodbc"))
from backend.phase5.run import _export_worker_serve, _open_copy_path


def handle(job):
    print(f"pid {os.getpid()} exporting {job['pmnum']}", flush=True)
    action = job.get("fake")
    if action == "fail":
        raise RuntimeError("Layout 'Vertical Layout' not found")
    if action == "crash":
        os._exit(3)
    if action == "keep_copy_and_crash":
        # a process killed while it kept a copy of the project open
        open(_open_copy_path(job["pdf_path"], 1), "w").close()
        os._exit(3)
    if action == "crash_once" and not os.path.exists(job["flag"]):
        open(job["flag"], "w").close()
        os._exit(3)
    with open(job["pdf_path"], "w") as f:
        f.write("pdf")


_export_worker_serve(handle)

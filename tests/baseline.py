"""The Phase 4 code as it stood before the lateral and parcel rewrite.

The equivalence tests run the old and new code on the same generated inputs
and require identical results.  Rather than keep an 1,800-line copy of the old
code in the repository, it is read from git history at BASELINE_COMMIT and
loaded as a module.

Returns None when that is not possible -- no git on the machine, or a copy of
the project without its .git folder, such as the shared network folder -- and
the tests that need it skip rather than fail.

The baseline still imports the current backend.phase4.network_cache and
config_loader, so a change to those that drops a name the old code imports
will need this commit moved forward.
"""
import importlib.util
import os
import subprocess
import tempfile

# The last commit before 5cb1eb9 ("Select laterals and parcels without
# schema-changing tool calls").
BASELINE_COMMIT = "1cfee6f"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_baseline():
    try:
        source = subprocess.run(
            ["git", "-C", _ROOT, "show", f"{BASELINE_COMMIT}:backend/phase4/run.py"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    folder = tempfile.mkdtemp(prefix="hfml_baseline_")
    path = os.path.join(folder, "phase4_run_baseline.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(source)
    spec = importlib.util.spec_from_file_location("phase4_run_baseline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

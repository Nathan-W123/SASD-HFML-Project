import importlib, os, sys, tempfile, textwrap, time

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond: fails.append(name)

# Pull the real reload block out of the toolbox, so this tests what ships.
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "SASD_HFML.pyt"),
           encoding="utf-8").read().splitlines()
start = next(i for i, l in enumerate(src) if "phase_names = {name for name, _ in PHASES}" in l)
end = next(i for i in range(start, len(src)) if "importlib.reload(module)" in src[i])
block = textwrap.dedent("\n".join(src[start:end + 1]))

root = tempfile.mkdtemp()
pkg = os.path.join(root, "backend", "phase4")
os.makedirs(pkg)
helper = os.path.join(pkg, "network_cache.py")
runmod = os.path.join(pkg, "run.py")
with open(helper, "w") as f: f.write("VALUE = 'old'\n")
with open(runmod, "w") as f: f.write("from backend.phase4.network_cache import VALUE\n")
sys.path.insert(0, root)

PHASES = [("backend.phase4.run", "Trace")]
import backend.phase4.run as r
check("starts on the old helper", r.VALUE == "old", r.VALUE)

# Edit the helper as a deploy would, then do what the toolbox does on Run.
time.sleep(1.1)  # make sure mtime moves so the bytecode cache is not reused
with open(helper, "w") as f: f.write("VALUE = 'new'\n")

# Old behaviour: reload the phase only -> still old.
importlib.reload(importlib.import_module("backend.phase4.run"))
check("phase-only reload misses the helper (the bug)",
      sys.modules["backend.phase4.run"].VALUE == "old",
      sys.modules["backend.phase4.run"].VALUE)

# New behaviour: helpers first, then phases.
exec(block, {"PHASES": PHASES, "sys": sys, "importlib": importlib})
modules = [importlib.reload(importlib.import_module(n)) for n, _ in PHASES]
check("helper-then-phase reload picks up the edit", modules[0].VALUE == "new", modules[0].VALUE)

check("namespace packages are skipped, not reloaded into errors",
      "backend" in sys.modules and "backend.phase4" in sys.modules)

print()
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)

"""Run every test script in this folder and report.

    python tests/run_all.py            # all tests (~2 min)
    python tests/run_all.py --slow     # also re-run the comparison with deliberate bugs

Each test is a plain script that prints its checks and ends with a
"FAILURES: ..." line, exiting non-zero if any check failed.  They need no
ArcGIS: arcpy is replaced by stub_arcpy, a small stand-in with real 2D geometry
and arcpy's selection rules.  Use ArcGIS Pro's Python or any Python 3.9+.

Exits non-zero if any test fails.
"""
import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main():
    scripts = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    if "--slow" in sys.argv:
        scripts.append(os.path.join(HERE, "mutation_check.py"))
    failed = []
    started = time.time()
    for script in scripts:
        name = os.path.splitext(os.path.basename(script))[0]
        t = time.time()
        result = subprocess.run([sys.executable, script], cwd=ROOT, capture_output=True, text=True)
        lines = result.stdout.strip().splitlines()
        summary = lines[-1] if lines else ""
        status = "ok" if result.returncode == 0 else "FAIL"
        print(f"{status:<5} {name:<26} {time.time() - t:6.1f}s  {summary}")
        if result.returncode:
            failed.append(name)
            print(result.stdout[-3000:])
            print(result.stderr[-3000:])
    print(f"\n{len(scripts) - len(failed)} of {len(scripts)} passed in {time.time() - started:.0f}s")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

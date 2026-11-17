"""Prove the equivalence test can fail: break the new code three ways and
check each is caught.  Slow (~4 min); run via run_all.py --slow."""
import runpy, sys, io, contextlib
import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
import backend.phase4.run as NEW

real_feet, real_ends, real_tol = NEW.LL_SEARCH_FEET, NEW._endpoints, NEW._DISTANCE_TOLERANCE
mutations = {
    "search distance 0.5 ft instead of 1": lambda: setattr(NEW, "LL_SEARCH_FEET", 0.5),
    "only the first end of each lateral": lambda: setattr(NEW, "_endpoints",
        lambda g: [(g.firstPoint.X, g.firstPoint.Y)]),
    "no XY tolerance": lambda: setattr(NEW, "_DISTANCE_TOLERANCE", 0.0),
}
missed = []
for label, apply in mutations.items():
    apply()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_equivalence.py"),
                           run_name="__main__")
            code = 0
        except SystemExit as e:
            code = e.code
    fails = out.getvalue().count("FAIL ")
    print(f"{'CAUGHT' if code else 'MISSED'}: {label} ({fails} failing checks)")
    if not code:
        missed.append(label)
    NEW.LL_SEARCH_FEET, NEW._endpoints, NEW._DISTANCE_TOLERANCE = real_feet, real_ends, real_tol

print()
print("FAILURES: " + (", ".join(f"missed: {m}" for m in missed) if missed else "none"))
sys.exit(1 if missed else 0)

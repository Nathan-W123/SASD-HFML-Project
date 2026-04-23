import importlib
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


PHASE_MODULES = {
    "phase1": "backend.phase1.run",
    "phase2": "backend.phase2.run",
    "phase3": "backend.phase3.run",
    "phase4": "backend.phase4.run",
    "phase5": "backend.phase5.run",
}


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in PHASE_MODULES:
        print("Usage: python backend/run_phase.py <phase1|phase2|phase3|phase4|phase5>", file=sys.stderr)
        return 2

    phase_key = sys.argv[1]
    module = importlib.import_module(PHASE_MODULES[phase_key])
    log_fn = lambda msg: print(msg, flush=True)

    try:
        module.run(log_fn)
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

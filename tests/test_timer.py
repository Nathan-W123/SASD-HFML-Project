import sys, time, types
import os, sys
# Run from anywhere: put this folder (stand-ins, shared helpers) and the
# project root (backend, config_loader) on the import path.
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)),
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
from backend.phase4.run import _StepTimer

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " :: " + str(detail)))
    if not cond: fails.append(name)

lines = []
t = _StepTimer(lines.append)

t.start_hfml()
with t.step("slow step"):
    time.sleep(0.05)
with t.step("fast step"):
    pass
t.log_hfml("9895")
check("per-HFML line names the HFML", "Timing for 9895" in lines[0], lines)
check("slowest step is listed first", lines[0].index("slow step") < lines[0].index("fast step"), lines[0])

# a step that raises must still be timed, and the exception must propagate
raised = False
try:
    with t.step("exploding step"):
        raise ValueError("boom")
except ValueError:
    raised = True
check("exception still propagates", raised)
check("failed step is still timed", "exploding step" in t.run_totals, t.run_totals)

# a second HFML must not inherit the first one's numbers
lines.clear()
t.start_hfml()
with t.step("only this one"):
    pass
t.log_hfml("39935")
check("per-HFML totals reset", "slow step" not in lines[0], lines)

# run totals accumulate across HFMLs
check("run totals accumulate", "slow step" in t.run_totals and "only this one" in t.run_totals,
      sorted(t.run_totals))

lines.clear()
t.log_run()
check("run summary printed", any("Phase 4 time by step" in l for l in lines), lines)
check("run summary shows percentages", any("%)" in l for l in lines), lines)
check("run summary covers every step",
      all(any(label in l for l in lines) for label in t.run_totals), lines)

# repeated use of one label sums rather than overwrites
t2 = _StepTimer(lambda m: None)
t2.start_hfml()
for _ in range(3):
    with t2.step("repeat"):
        time.sleep(0.01)
check("repeated label accumulates", t2.run_totals["repeat"] >= 0.03, t2.run_totals)

# nothing timed -> no output, no crash
lines.clear()
t3 = _StepTimer(lines.append)
t3.start_hfml(); t3.log_hfml("x"); t3.log_run()
check("silent when nothing was timed", lines == [], lines)

# ---- sub-steps ------------------------------------------------------------
from backend.phase4.run import _NullTimer

lines.clear()
t4 = _StepTimer(lines.append)
t4.start_hfml()
with t4.step("select LLs"):
    with t4.substep("LL: open layers"):
        time.sleep(0.02)
    with t4.substep("LL: select by location over the whole layer"):
        time.sleep(0.03)
t4.log_hfml("4364")
check("sub-steps do not inflate the headline total",
      "select LLs" in lines[0] and "LL: open layers" not in lines[0], lines)
check("sub-steps get their own line",
      any("Inside those steps:" in l and "LL: open layers" in l for l in lines), lines)

headline = sum(t4.run_totals.values())
check("headline counts only outer steps", abs(headline - t4.run_totals["select LLs"]) < 1e-9,
      t4.run_totals)
check("sub totals kept separately", "LL: open layers" in t4.sub_totals, t4.sub_totals)

lines.clear()
t4.log_run()
check("run summary separates sub-steps",
      any("Inside those steps (already counted above)" in l for l in lines), lines)
check("percentages sum over outer steps only",
      any("select LLs" in l and "(100%)" in l for l in lines), lines)

# a sub-step that raises is still recorded and still propagates
try:
    with t4.substep("boom"):
        raise ValueError("x")
except ValueError:
    pass
check("raising sub-step recorded", "boom" in t4.sub_totals, t4.sub_totals)

# the null timer stands in with no behaviour
n = _NullTimer()
ran = False
with n.substep("anything"):
    ran = True
check("null timer runs the body", ran)

print()
print("FAILURES: " + (", ".join(fails) if fails else "none"))
sys.exit(1 if fails else 0)

"""Gate on hold discharge (day-15 row 1.7).

Every hold issued to the simulator must reach one of four end states:
terminated by a named event, still in force when the run ends, superseded by
a later directive for the same train, or berthed with a logged occupancy
block on its exit. A hold that expires with none of those has no discharge
path -- STAND_ON_MAIN's branch needs standing_on_main, the loop branch needs
in_loop, and a HOLD_AT_LOOP on a train that reaches neither is latched.

Written against directive identity (hold_seq), never against
train.hold_station_id is None. HARNESS-NOTES records three predicates that
misreported on correctly-expiring holds; a fourth misreported on day 15 by
treating "berthed, main occupied" as a failure. A train in a loop cannot pull
out onto an occupied running line, and that is the railway working.

Asserts on the shipped fraction only. HOLD_GATE_REPORT names fractions that
are measured and printed but never fail the test -- 0.001 approximates the
pre-day-15 emitter and is expected to latch, which is what shows the gate
can go red rather than merely being green.

  HOLD_GATE_SEEDS="1 2 3 ... 15"   full gate evaluation
  HOLD_GATE_REPORT="0.001"         add the discrimination arm
"""
import json
import os
import subprocess
import sys
import tempfile

SEEDS = [int(s) for s in os.getenv("HOLD_GATE_SEEDS", "3 7 12").split()]
ASSERTED = os.getenv("HOLD_GATE_FRACTION", "0.35")
REPORTED = [f for f in os.getenv("HOLD_GATE_REPORT", "").split() if f]
TICKS = int(os.getenv("HOLD_GATE_TICKS", "1080"))
RUN_END_S = TICKS * 10.0

TERMINAL = {
    "released", "superseded_by_regulate", "superseded_by_hold",
    "recycled", "abandoned_astern", "discharged_stand", "discharged_loop",
}


def launch(seed, fraction):
    tmp = tempfile.gettempdir()
    trace = os.path.join(tmp, f"holdgate-{seed}-{fraction}.jsonl")
    csv_out = os.path.join(tmp, f"holdgate-{seed}-{fraction}.csv")
    if os.path.exists(trace):
        os.remove(trace)
    env = dict(os.environ)
    env["ENGINE"] = "global"
    env["MIN_REGULATION_FRACTION"] = fraction
    env["SIM_TRACE_HOLDS"] = trace
    # Runs are launched in parallel, so the wall clock is contended and
    # GLOBAL_TIER_BUDGET_S truncates tiers unevenly -- seed 12 produced 25,
    # 15 and 15 issued holds across three invocations of this file. Lift the
    # wall-clock budget so only GLOBAL_DET_BUDGET binds; deterministic time is
    # reproducible across machines and across load.
    env["GLOBAL_TIER_BUDGET_S"] = os.getenv("HOLD_GATE_TIER_BUDGET", "1000")
    proc = subprocess.Popen(
        [sys.executable, "tests/harness.py", "--seed", str(seed),
         "--arm", "A", "--ticks", str(TICKS), "--csv", csv_out],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return seed, fraction, trace, proc


def classify(trace):
    events = [json.loads(line) for line in open(trace)]
    issued = {e["seq"]: e for e in events if e["writer"].startswith("issued_")}
    term = {e["seq"] for e in events if e["writer"] in TERMINAL}
    berthed = {e["seq"] for e in events if e["writer"] == "berthed"}
    blocked = {e["seq"] for e in events if e["writer"] == "release_blocked"}
    latched = []
    for seq, issue in issued.items():
        if seq in term or seq in blocked:
            continue
        expiry = issue["sim_s"] + (issue["expires_in"] or 0.0)
        if expiry > RUN_END_S:
            continue
        latched.append(
            f"seq {seq} {issue['train']} issued t{issue['tick']} "
            f"expired {expiry:.0f}s, "
            f"{'berthed' if seq in berthed else 'never berthed'}"
        )
    return len(issued), len(term), len(blocked), latched


fail = []
running = [launch(s, f) for f in [ASSERTED] + REPORTED for s in SEEDS]
print(f"{len(running)} runs in parallel, {TICKS} ticks each\n")

for seed, fraction, trace, proc in running:
    proc.wait()
    tag = "ASSERT" if fraction == ASSERTED else "report"
    if proc.returncode != 0:
        msg = f"seed {seed} f={fraction}: harness exit {proc.returncode}"
        print(f"  {tag} {msg}")
        fail.append(msg)
        continue
    if not os.path.exists(trace):
        msg = f"seed {seed} f={fraction}: no trace -- SIM_TRACE_HOLDS ignored"
        print(f"  {tag} {msg}")
        fail.append(msg)
        continue
    n_iss, n_term, n_block, latched = classify(trace)
    print(f"  {tag} seed {seed:>2} f={fraction:<6} {n_iss:>2} issued  "
          f"{n_term:>2} terminated  {n_block:>2} exit-blocked  "
          f"{len(latched)} latched")
    for row in latched:
        print(f"         {row}")
        if fraction == ASSERTED:
            fail.append(f"seed {seed} f={fraction}: {row}")

print()
if fail:
    print("HOLD-FAIL: " + "; ".join(fail))
    sys.exit(1)
print("HOLD-PASS")
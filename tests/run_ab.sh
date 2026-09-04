#!/usr/bin/env bash
# Day-4 A/B sweep: the enumerate engine with and without approval, five seeds.
# Not a test suite -- no assertions except the determinism gate, which is a
# hard stop because every number after it is meaningless if it fails.
#
#   ./tests/run_ab.sh                       full run, 1080 ticks, seeds 1-5
#   TICKS=60 SEEDS="1 2" ./tests/run_ab.sh  smoke
#   DETERMINISM_TICKS=120 ./tests/run_ab.sh cheaper gate, full sweep
set -uo pipefail

PY="${PYTHON:-python3}"
SCEN="${SCENARIO_PATH:-data/scenario10.json}"
CSV="${CSV:-docs/baselines/ab-enumerate.csv}"
ENVLOG="${CSV%.csv}.env.txt"
TICKS="${TICKS:-1080}"
DETERMINISM_TICKS="${DETERMINISM_TICKS:-$TICKS}"
SEEDS="${SEEDS:-1 2 3 4 5}"
ARMS="${ARMS:-A B}"
export SCENARIO_PATH="$SCEN"

# ---------------------------------------------------------------------------
# Preflight, deliberately OUTSIDE any subshell: five seconds here beats a
# ninety-minute sweep that returns ten tracebacks and an empty CSV.
# ---------------------------------------------------------------------------
echo "### 0. preflight"
$PY - <<'PY'
import ast, sys
sys.path[:0] = ["shared", "ai-engine", "simulator", "tests"]

FILES = ("ai-engine/optimizer.py", "ai-engine/detector.py", "ai-engine/main.py",
         "simulator/injector.py", "tests/verify_scenario.py",
         "tests/count_refusals.py", "tests/harness.py")
for f in FILES:
    try:
        ast.parse(open(f).read())
    except (SyntaxError, IndentationError) as exc:
        print(f"  SYNTAX  {f}: {exc}")
        sys.exit(1)
    except FileNotFoundError:
        print(f"  MISSING {f}")
        sys.exit(1)
print(f"  parsed {len(FILES)} files")

try:
    import optimizer as o
    import main as m
    import count_refusals as c
    import harness as h
except Exception as exc:
    print(f"  IMPORT  failed: {exc!r}")
    sys.exit(1)

need = ("MAX_TRAINS_ENUMERATED", "ENUMERATION_BUDGET_S",
        "SOLVER_TIME_LIMIT_S", "SOLVER_WORKERS")
missing = [n for n in need if not hasattr(o, n)]
if missing:
    print("  MISSING from optimizer: " + ", ".join(missing))
    sys.exit(1)
if not callable(getattr(m, "solvable_conflicts", None)):
    print("  MISSING from main: solvable_conflicts")
    sys.exit(1)
# The whole point of the day-4 refactor: the harness must not own a second
# copy of the refusal/coverage rules.
if not callable(getattr(c, "audit_plans", None)):
    print("  MISSING from count_refusals: audit_plans")
    sys.exit(1)
if h.audit_plans is not c.audit_plans:
    print("  DRIFT   harness is not using count_refusals.audit_plans")
    sys.exit(1)

# Both wall-clock deadlines must be lifted, or step 8 is unwinnable: a budget
# break makes the RESULT depend on machine load, not just the timing column.
if o.ENUMERATION_BUDGET_S < 1e6:
    print(f"  BROKEN  ENUMERATION_BUDGET_S={o.ENUMERATION_BUDGET_S} still in force")
    sys.exit(1)
if o.SOLVER_DETERMINISTIC_TIME <= 0:
    print("  BROKEN  SOLVER_DETERMINISTIC_TIME=0; a wall-clock-only budget "
          "cannot reproduce across machines")
    sys.exit(1)

print(f"  cap={o.MAX_TRAINS_ENUMERATED} workers={o.SOLVER_WORKERS} "
      f"budget={o.ENUMERATION_BUDGET_S} limit={o.SOLVER_TIME_LIMIT_S}")
print(f"  approval rule={h.APPROVAL_RULE}  columns={len(h.CSV_COLUMNS)}")
PY
if [ $? -ne 0 ]; then
  echo
  echo "Preflight failed. Fix the above before measuring -- no rows written."
  exit 1
fi
echo "  preflight OK"
echo

# ---------------------------------------------------------------------------
# Step 8. The load-bearing test. Seed 1 arm A, twice, every column compared
# except the wall-clock one. A mismatch means the harness has nondeterminism in
# it and every row it writes afterwards is noise.
# ---------------------------------------------------------------------------
echo "### 1. determinism gate (seed 1, arm A, twice, ${DETERMINISM_TICKS} ticks)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

$PY tests/harness.py --seed 1 --arm A --ticks "$DETERMINISM_TICKS" \
    --csv "$TMP/run1.csv" --progress 0 >/dev/null || exit 1
$PY tests/harness.py --seed 1 --arm A --ticks "$DETERMINISM_TICKS" \
    --csv "$TMP/run2.csv" --progress 0 >/dev/null || exit 1

$PY - "$TMP/run1.csv" "$TMP/run2.csv" <<'PY'
import csv, sys
sys.path[:0] = ["shared", "ai-engine", "simulator", "tests"]
from harness import NONDETERMINISTIC_COLUMNS

a = list(csv.DictReader(open(sys.argv[1])))[0]
b = list(csv.DictReader(open(sys.argv[2])))[0]
bad = [k for k in a if k not in NONDETERMINISTIC_COLUMNS and a[k] != b[k]]
for k in bad:
    print(f"  MISMATCH {k}: {a[k]!r} vs {b[k]!r}")
if bad:
    print("\n  The harness is nondeterministic. Do not rationalise a 'small'")
    print("  difference -- find it. Usual suspects: a wall-clock deadline still")
    print("  in force, or max()/min() over a set where the key ties.")
    sys.exit(1)
skipped = ", ".join(NONDETERMINISTIC_COLUMNS)
print(f"  identical on every column except {skipped} "
      f"({a['max_solve_ms']} vs {b['max_solve_ms']} ms, wall clock, expected)")
PY
if [ $? -ne 0 ]; then
  echo
  echo "Determinism gate failed. Nothing appended to $CSV."
  exit 1
fi
echo

# ---------------------------------------------------------------------------
# Provenance. The CSV holds results; this holds the configuration that produced
# them, so a baseline can never be mistaken for a live-demo run.
# ---------------------------------------------------------------------------
mkdir -p "$(dirname "$CSV")"
{
  echo "run      : $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "host     : $(uname -srm)"
  echo "regfrac  : $($PY -c 'import sys; sys.path[:0]=["shared"]; import railsim.kinematics as k; print(k.MIN_REGULATION_FRACTION)')"
  echo "python   : $($PY -V 2>&1)"
  echo "git      : $(git rev-parse --short HEAD 2>/dev/null || echo '(not a repo)')"
  echo "branch   : $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '-')"
  echo "scenario : $SCEN"
  echo "ticks    : $TICKS"
  echo "seeds    : $SEEDS"
  echo "arms     : $ARMS"
  $PY - <<'PY'
import sys
sys.path[:0] = ["shared", "ai-engine", "simulator", "tests"]
import optimizer as o, harness as h
print(f"engine   : {h.ENGINE}")
print(f"cap      : {o.MAX_TRAINS_ENUMERATED}")
print(f"workers  : {o.SOLVER_WORKERS}")
print(f"budget   : {o.ENUMERATION_BUDGET_S}   (lifted for reproducibility)")
print(f"limit    : {o.SOLVER_TIME_LIMIT_S}    (lifted for reproducibility)")
print(f"rule     : {h.APPROVAL_RULE}")
print(f"jitter   : +/-{h.KM_JITTER} km, +/-{h.DELAY_JITTER} s")
if h.ENGINE == "global":
    import optimizer_global as g
    print(f"holdtier : {g.GLOBAL_HOLD_TIER}")
    print(f"tierbudg : {g.GLOBAL_TIER_BUDGET_S}")
    print(f"detbudg  : {g.GLOBAL_DET_BUDGET}")
    print(f"maxstops : {g.GLOBAL_MAX_STOPS}")
    print(f"starve   : {g.GLOBAL_STARVATION_THRESHOLD_S}")
    print(f"holdcap  : {g.GLOBAL_HOLD_CAP_MULTIPLIER}")
PY
} | tee "$ENVLOG"
echo
echo "provenance written to $ENVLOG"
echo

# ---------------------------------------------------------------------------
echo "### 2. sweep"
rows=0
for seed in $SEEDS; do
  for arm in $ARMS; do
    echo "--- seed $seed arm $arm ---"
    if $PY tests/harness.py --seed "$seed" --arm "$arm" --ticks "$TICKS" \
         --csv "$CSV" --progress 240; then
      rows=$((rows + 1))
    else
      echo "  FAILED seed=$seed arm=$arm -- continuing; the CSV will be short"
    fi
  done
done

echo
echo "appended $rows rows to $CSV"
echo
echo "Sanity read before you commit (step 10):"
echo "  * arm A beats arm B on premier_delay_s on every seed. If it does not,"
echo "    either the harness is wrong or the engine does not help -- find out"
echo "    which before writing a global solver."
echo "  * seed 0 is the row to check against the day-2 report. Run it"
echo "    separately: $PY tests/harness.py --seed 0 --arm B --ticks $TICKS"
echo "    and compare uncovered_trains_t0 / contradictory_instructions_t0"
echo "    against tests/count_refusals.py. Seeds 1-5 are perturbed and will"
echo "    not reproduce those numbers."
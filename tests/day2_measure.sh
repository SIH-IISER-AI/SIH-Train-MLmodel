#!/usr/bin/env bash
# Day-2 measurement sweep. Not a test suite -- no assertions, no pass/fail.
# Writes one report; send it to the advisor verbatim.
set -uo pipefail

if command -v timeout >/dev/null 2>&1; then TIMEOUT="timeout 3600"
elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT="gtimeout 3600"
else TIMEOUT=""; fi

SCEN="${1:-data/scenario10.json}"
OUT="${2:-day2-report.txt}"
PY="${PYTHON:-python3}"

if command -v timeout >/dev/null 2>&1; then TIMEOUT="timeout 3600"
elif command -v gtimeout >/dev/null 2>&1; then TIMEOUT="gtimeout 3600"
else TIMEOUT=""; fi

# Preflight, deliberately OUTSIDE the tee block below: that block runs in a
# subshell, so an exit inside it would not stop the script. Five seconds here
# beats a container run that returns four tracebacks and an empty report.
echo "### 0. preflight"
$PY - <<'PY'
import ast, sys
sys.path[:0] = ["shared", "ai-engine", "simulator"]

FILES = ("ai-engine/optimizer.py", "ai-engine/detector.py", "ai-engine/main.py",
         "tests/verify_scenario.py", "tests/bench_solve.py",
         "tests/bench_cap_sweep.py", "tests/count_refusals.py")
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

print(f"  cap={o.MAX_TRAINS_ENUMERATED} budget={o.ENUMERATION_BUDGET_S} "
      f"limit={o.SOLVER_TIME_LIMIT_S} workers={o.SOLVER_WORKERS}")
PY
if [ $? -ne 0 ]; then
  echo
  echo "Preflight failed. Fix the above before measuring -- no report written."
  exit 1
fi

# Section 3 is worthless if the env override is shadowed by a second
# definition: the sweep would silently measure the 5 s budget at every cap.
probe=$(MAX_TRAINS_ENUMERATED=7 ENUMERATION_BUDGET_S=99 $PY -c "
import sys; sys.path[:0] = ['shared','ai-engine']
import optimizer as o; print(o.MAX_TRAINS_ENUMERATED, o.ENUMERATION_BUDGET_S)
" 2>/dev/null)
if [ "$probe" != "7 99.0" ]; then
  echo "  BROKEN  env override not effective (got '${probe:-<error>}', want '7 99.0')"
  echo "          a duplicate constant later in optimizer.py is shadowing it"
  exit 1
fi
echo "  env override effective"
echo "  preflight OK"
echo

{
  echo "=============================================================="
  echo "day-2 measurement sweep"
  echo "scenario : $SCEN"
  echo "date     : $(date -Is)"
  echo "python   : $($PY -V 2>&1)"
  echo "=============================================================="

  echo; echo "### 1. scenario verification"
  $PY tests/verify_scenario.py "$SCEN"

  echo; echo "### 2. solver benchmark (cap=5, production default)"
  $PY tests/bench_solve.py "$SCEN"

  echo; echo "### 3. cap sweep (budget disabled -- measuring the engine, not the cap)"
  ENUMERATION_BUDGET_S=100000 $TIMEOUT $PY tests/bench_cap_sweep.py "$SCEN" 4,5,6,7,8 \
    || echo "  (sweep aborted -- report whichever caps completed)"

  echo; echo "### 4. predicted directive refusals (OPT-1, tick 0)"
  $PY tests/count_refusals.py "$SCEN"

  echo; echo "=== end of sweep ==="
} 2>&1 | tee "$OUT"

echo
echo "report written to $OUT"
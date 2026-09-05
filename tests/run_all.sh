#!/usr/bin/env bash
# Run every test from the REPO ROOT and write one pasteable report.
#   ./tests/run_all.sh          -> report at run-report.txt
#   ./tests/run_all.sh out.txt  -> report at out.txt
#
# The tests are plain scripts, not pytest: they print PASS/FAIL and exit 0
# either way. This wrapper is what turns them into a pass/fail signal.

set -uo pipefail

REPORT="${1:-run-report.txt}"

if [ ! -f "data/network.json" ]; then
  echo "Run this from the repo root (data/network.json not found)." >&2
  exit 2
fi

if [ -d ".venv" ] && [ -z "${VIRTUAL_ENV:-}" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

: > "$REPORT"
exec > >(tee -a "$REPORT") 2>&1

echo "=============================================================="
echo "ENVIRONMENT"
echo "=============================================================="
date -u +"utc      %Y-%m-%d %H:%M:%S"
echo "python   $(python3 -V 2>&1)"
echo "venv     ${VIRTUAL_ENV:-<none>}"
echo "git      $(git rev-parse --short HEAD 2>/dev/null || echo '<not a git repo>') on $(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
echo "dirty    $(git status --porcelain 2>/dev/null | wc -l) file(s) modified"
echo
echo "--- pinned deps ---"
pip freeze 2>/dev/null | grep -Ei '^(redis|pydantic|ortools|railsim)' || echo "  (pip freeze returned nothing)"
echo
echo "--- requirements.txt as committed ---"
cat ai-engine/requirements.txt
echo

echo "=============================================================="
echo "FIX VERIFICATION (grep assertions on the eight changes)"
echo "=============================================================="
check() {  # check <label> <expect: present|absent> <pattern> <file>
  local label="$1" expect="$2" pat="$3" file="$4" hits
  hits=$(grep -c -- "$pat" "$file" 2>/dev/null || true)
  hits=${hits:-0}
  if { [ "$expect" = "absent" ] && [ "$hits" -eq 0 ]; } || \
     { [ "$expect" = "present" ] && [ "$hits" -gt 0 ]; }; then
    echo "  OK    $label"
  else
    echo "  BROKEN $label  (expected $expect, found $hits in $file)"
    FIXES_BROKEN=$((FIXES_BROKEN + 1))
  fi
}
dupe_check() {  # dupe_check <label> <constant> <file>
  local n
  n=$(grep -c "^$2 = " "$3" 2>/dev/null || true)
  n=${n:-0}
  if [ "$n" -eq 1 ]; then
    echo "  OK    $1 defined once"
  else
    echo "  BROKEN $1 defined $n times in $3"
    FIXES_BROKEN=$((FIXES_BROKEN + 1))
  fi
}
FIXES_BROKEN=0
check "F1  scikit-learn gone"          absent  "scikit-learn"                    ai-engine/requirements.txt
check "F1b ortools pinned"             present "ortools=="                       ai-engine/requirements.txt
check "F2  probe7 gone"                absent  "probe7"                          ai-engine/main.py
check "F3  occupants string gone"      absent  "occupants = "                    ai-engine/detector.py
check "F4  raw speed to solver"        present 'telemetry\["speed_kmh"\]),'      ai-engine/detector.py
check "F5  solver limit 0.25"          present "SOLVER_TIME_LIMIT_S = 0.25"      ai-engine/optimizer.py
check "F5  single search worker"       present "SOLVER_WORKERS = 1"              ai-engine/optimizer.py
check "F6  budget default still 5.0"   present "ENUMERATION_BUDGET_S., .5.0"     ai-engine/optimizer.py
check "F6b cap default still 5"        present "MAX_TRAINS_ENUMERATED., .5"      ai-engine/optimizer.py
check "F6  time imported"              present "^import time"                    ai-engine/optimizer.py
check "D8  chaining present"           present "def chain_links"                 ai-engine/optimizer_global.py
check "D8  slack against ready"        present "slack\[key\] == entry\[key\] - ready" ai-engine/optimizer_global.py
check "D9  exemption removed"          absent  "EXPECTED (pre-chaining)"          tests/test_global_encoding.py
check "D10 lexicographic descent"      present "lexicographic"                   ai-engine/optimizer_global.py
check "D11 motivating_resource_id"     present "motivating_resource_id"          ai-engine/optimizer_global.py
check "D12 per-tier budget"            present "GLOBAL_TIER_BUDGET_S"            ai-engine/optimizer_global.py
check "D12 starvation threshold"       present "GLOBAL_STARVATION_THRESHOLD_S"   ai-engine/optimizer_global.py
check "F7  deadline hoisted"           present "enumeration_deadline = time"     ai-engine/optimizer.py
check "F8  unconditional priority sort" absent "        )\[:MAX_TRAINS_ENUMERATED\]" ai-engine/optimizer.py
check "D11 replay gate exists"  present "submit_directive"  tests/test_directive_replay.py
check "D14 floor gate exists"            present "regulated_speed_kmh"                tests/test_regulation_floor.py
check "D14 gate discriminates"           present "min_fraction=0.0"                   tests/test_regulation_floor.py
check "D14 one fraction, env-overridable" present 'MIN_REGULATION_FRACTION", "0.35"'  shared/railsim/kinematics.py
check "D14 emitter saturates"            present "min_fraction: float = MIN_REGULATION_FRACTION" shared/railsim/kinematics.py
check "D15 no separate floor constant"   absent  "REGULATION_FLOOR_FRACTION"          shared/railsim/kinematics.py
check "D15 discharge gate exists"        present "release_blocked"                    tests/test_hold_discharge.py
check "D15 hold identity present"        present "hold_seq"                           simulator/injector.py
check "D15 blocked release logged"       present "release_blocked"                    simulator/injector.py
# A duplicated constant raises no error -- the later definition silently wins.
# This is how the env override for the cap sweep died without a traceback.
dupe_check "MIN_REGULATION_FRACTION"    MIN_REGULATION_FRACTION    shared/railsim/kinematics.py
dupe_check "MAX_TRAINS_ENUMERATED" MAX_TRAINS_ENUMERATED ai-engine/optimizer.py
dupe_check "ENUMERATION_BUDGET_S"  ENUMERATION_BUDGET_S  ai-engine/optimizer.py
dupe_check "SOLVER_TIME_LIMIT_S"   SOLVER_TIME_LIMIT_S   ai-engine/optimizer.py
dupe_check "SOLVER_WORKERS"        SOLVER_WORKERS        ai-engine/optimizer.py
echo

echo "=============================================================="
echo "SYNTAX"
echo "=============================================================="
python3 - <<'PY'
import ast, sys
bad = 0
for f in ("ai-engine/detector.py", "ai-engine/optimizer.py", "ai-engine/main.py",
          "ai-engine/optimizer_global.py",
          "simulator/injector.py", "shared/railsim/kinematics.py",
          "shared/railsim/topology.py"):
    try:
        ast.parse(open(f).read())
        print(f"  OK     {f}")
    except SyntaxError as e:
        print(f"  SYNTAX {f}: {e}")
        bad += 1
sys.exit(1 if bad else 0)
PY
echo

# macOS ships no coreutils `timeout`. Use gtimeout if brew installed it, else
# run without a watchdog rather than reporting exit 127 as a test failure.
if command -v timeout >/dev/null 2>&1; then
  TIMEOUT_CMD="timeout 300"
elif command -v gtimeout >/dev/null 2>&1; then
  TIMEOUT_CMD="gtimeout 300"
else
  TIMEOUT_CMD=""
  echo "note: no timeout/gtimeout on PATH -- tests run unbounded."
  echo "      brew install coreutils   gives you gtimeout."
  echo
fi

FAILED_TESTS=()
run_test() {
  local name="$1"
  echo "=============================================================="
  echo "TEST  $name"
  echo "=============================================================="
  local start out rc
  start=$SECONDS
  out=$(${TIMEOUT_CMD} python3 "tests/$name" 2>&1)
  rc=$?
  echo "$out"
  printf -- "--- %s: exit=%d  wall=%ds\n" "$name" "$rc" "$((SECONDS - start))"
  if [ $rc -ne 0 ] || echo "$out" | grep -qE 'FAIL|Traceback'; then
    FAILED_TESTS+=("$name")
    echo "--- $name: FAILED"
  else
    echo "--- $name: passed"
  fi
  echo
}

for t in test_speed_parity.py test_stand.py test_hold_release.py \
         test_policy_cap.py test_hysteresis.py \
         test_chaining.py test_global_hold.py test_global_encoding.py \
         test_descent.py test_directive_replay.py \
         test_regulation_floor.py test_hold_discharge.py; do
  run_test "$t"
done

echo "=============================================================="
echo "SUMMARY"
echo "=============================================================="
echo "fix checks broken: $FIXES_BROKEN"
if [ ${#FAILED_TESTS[@]} -eq 0 ]; then
  echo "tests failed:      0  -- all green"
else
  echo "tests failed:      ${#FAILED_TESTS[@]}  -- ${FAILED_TESTS[*]}"
fi
echo
echo "Report written to $REPORT"
[ $FIXES_BROKEN -eq 0 ] && [ ${#FAILED_TESTS[@]} -eq 0 ]
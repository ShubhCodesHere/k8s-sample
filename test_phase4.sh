#!/usr/bin/env bash
#
# test_phase4.sh — PROACTIVE healing test (Phase 2 -> 3 -> 4).
# Triggers a CPU spike and asserts the swarm restarts the pod fast (fast path,
# no LLM) BEFORE the pod ever crashes.
#
#   ./test_phase4.sh
#
# NVIDIA_API_KEY is OPTIONAL: cpu_spike/memory_leak take the LLM-free fast path.
# Heal is verified by the pod NAME changing to a fresh Running pod (delete makes a
# new pod with restartCount=0, so a restartCount bump never happens for a delete).

set -uo pipefail
cd "$(dirname "$0")"
REMOTE=/home/docker/ebpf_monitor.py
APP=victim-app

if [ -z "${NVIDIA_API_KEY:-}" ]; then
  echo "NOTE: NVIDIA_API_KEY not set — LLM path disabled; proactive fast path still works."
fi

ALERT_RESULT=FAIL;  ALERT_T="-"
FASTPATH_RESULT=FAIL
RESTART_RESULT=FAIL; RESTART_T="-"
BEFORE_RESULT=FAIL;  BEFORE_NOTE=""
HEAL_T="-"

green(){ printf "\033[32m%s\033[0m\n" "$1"; }
red(){ printf "\033[31m%s\033[0m\n" "$1"; }
blue(){ printf "\033[34m%s\033[0m\n" "$1"; }

PIPELINE_PID=""; SWARM_PID=""
cleanup() {
  blue "==> Stopping background processes"
  [ -n "$SWARM_PID" ] && kill "$SWARM_PID" 2>/dev/null
  [ -n "$PIPELINE_PID" ] && kill "$PIPELINE_PID" 2>/dev/null
  pkill -f "causal_engine.py" 2>/dev/null || true
  pkill -f "swarm.py" 2>/dev/null || true
  minikube ssh -- "sudo pkill -f ebpf_monitor.py" 2>/dev/null || true
}
trap cleanup EXIT

pod_name(){ kubectl get pods -l app=${APP} --no-headers -o custom-columns=N:.metadata.name 2>/dev/null | head -1; }

# Start the pipeline + swarm.
: > diagnoses.log; : > swarm_output.log
blue "==> Starting Snitch -> Brain pipeline + Swarm"
minikube cp ./ebpf_monitor.py "${REMOTE}" 2>/dev/null
( minikube ssh -- "sudo python3 ${REMOTE}" | python3 -u causal_engine.py ) > pipeline_output.log 2>&1 &
PIPELINE_PID=$!
python3 swarm.py > swarm_output.log 2>&1 &
SWARM_PID=$!

blue "==> Waiting 5s to initialize"
sleep 5

OLD_POD=$(pod_name)
echo "  current pod: ${OLD_POD}"

# Trigger the CPU spike.
blue "==> Triggering /stress (proactive trigger)"
POD_IP=$(kubectl get pod -l app=${APP} -o jsonpath='{.items[0].status.podIP}')
minikube ssh -- "curl -s --max-time 5 http://${POD_IP}:8000/stress" 2>/dev/null; echo

# Watch: record alert time and the pod replacement (PASS if <=10s), then let the
# new pod reach Running and the swarm finish its Timer block (up to ~25s total).
blue "==> Watching for proactive heal (restart <=10s; allow up to ~25s for full recovery)"
START=$(date +%s)
RESTART_DONE=0
for i in $(seq 1 25); do
  ELAPSED=$(( $(date +%s) - START ))
  if [ "$ALERT_RESULT" = FAIL ] && grep -q "New diagnosis received" swarm_output.log; then
    ALERT_RESULT=PASS; ALERT_T=$ELAPSED
  fi
  NEW_POD=$(pod_name)
  if [ "$RESTART_DONE" -eq 0 ] && [ -n "$NEW_POD" ] && [ "$NEW_POD" != "$OLD_POD" ]; then
    RESTART_T=$ELAPSED
    [ "$RESTART_T" -le 10 ] && RESTART_RESULT=PASS
    RESTART_DONE=1
  fi
  # once restarted, wait for the swarm to confirm Running + emit its Timer block
  if [ "$RESTART_DONE" -eq 1 ] && grep -q "Total time to heal:" swarm_output.log; then
    break
  fi
  sleep 1
done

# Fast path used?
grep -q "FAST PATH activated" swarm_output.log && FASTPATH_RESULT=PASS

# Before-crash: pod must NOT be in OOMKilled/Error/CrashLoop (CPU spike must not
# have killed it — proactive heal beats the crash).
NEW_POD=$(pod_name)
NEW_STATUS=$(kubectl get pods -l app=${APP} --no-headers -o custom-columns=S:.status.phase 2>/dev/null | head -1)
LASTREASON=$(kubectl get pods -l app=${APP} -o jsonpath='{.items[0].status.containerStatuses[0].lastState.terminated.reason}' 2>/dev/null)
CPU_SEEN=$(python3 -c "
import json
v='?'
for ln in open('diagnoses.log'):
    ln=ln.strip()
    if not ln: continue
    try: d=json.loads(ln)
    except: continue
    if d.get('metric')=='cpu_spike' and 'cpu_percent' in d: v=d['cpu_percent']
print(v)
" 2>/dev/null)
if [ "$NEW_STATUS" = "Running" ] && [ "$LASTREASON" != "OOMKilled" ]; then
  BEFORE_RESULT=PASS; BEFORE_NOTE="CPU was ${CPU_SEEN}%, pod survived"
else
  BEFORE_NOTE="pod status=${NEW_STATUS} lastReason=${LASTREASON:-none}"
fi

# Total time to heal (prefer the swarm's own Timer line).
HEAL_LINE=$(grep "Total time to heal:" swarm_output.log | tail -1 | grep -oE '[0-9]+' | head -1)
[ -n "$HEAL_LINE" ] && HEAL_T="$HEAL_LINE"

echo
blue "===== SWARM OUTPUT ====="
cat swarm_output.log

echo
blue "===== PROACTIVE HEALING TEST ====="
printf "Alert detected:        %s (%ss)\n" "$ALERT_RESULT" "$ALERT_T"
printf "Fast path activated:   %s\n" "$FASTPATH_RESULT"
printf "Pod restarted:         %s (%ss)\n" "$RESTART_RESULT" "$RESTART_T"
printf "Before crash:          %s (%s)\n" "$BEFORE_RESULT" "$BEFORE_NOTE"
printf "Total time to heal:    %ss\n" "$HEAL_T"
blue "=================================="

[ "$ALERT_RESULT" = PASS ] && [ "$FASTPATH_RESULT" = PASS ] \
  && [ "$RESTART_RESULT" = PASS ] && [ "$BEFORE_RESULT" = PASS ]

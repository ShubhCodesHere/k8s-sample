#!/usr/bin/env bash
#
# test_phase2.sh — automated test for the Snitch (Phase 2).
# Runs on your Mac. Starts the monitor inside the Minikube node, exercises the
# victim app, and checks the captured log for alerts.
#
#   chmod +x test_phase2.sh
#   ./test_phase2.sh
#
# NOTE: endpoints are triggered from INSIDE the node against the pod IP, instead
# of `minikube service --url`. On the Docker driver that command holds a tunnel
# open in the foreground (it hangs a script); curling the pod IP from the node is
# equivalent and reliable.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE=/home/docker/ebpf_monitor.py
LOG="${SCRIPT_DIR}/ebpf_output.log"
APP=victim-app

CPU_RESULT=FAIL
CRASH_RESULT=FAIL

green(){ printf "\033[32m%s\033[0m\n" "$1"; }
red(){ printf "\033[31m%s\033[0m\n" "$1"; }
blue(){ printf "\033[34m%s\033[0m\n" "$1"; }

MON_PID=""
cleanup() {
  blue "==> Stopping monitor"
  [ -n "$MON_PID" ] && kill "$MON_PID" 2>/dev/null
  # also kill the remote python in case the ssh child outlived the local pipe
  minikube ssh -- "sudo pkill -f ebpf_monitor.py" 2>/dev/null || true
}
trap cleanup EXIT

hit() {  # hit <path> : curl the victim pod from inside the node
  local path="$1" ip
  ip="$(kubectl get pod -l "app=${APP}" -o jsonpath='{.items[0].status.podIP}' 2>/dev/null)"
  [ -z "$ip" ] && { red "  could not get pod IP"; return 1; }
  echo "  -> curl http://${ip}:8000${path} (from node)"
  minikube ssh -- "curl -s --max-time 5 http://${ip}:8000${path}" 2>/dev/null
  echo
}

# --- Step 1: copy + start the monitor in the background ----------------------
blue "==> Copying monitor into node and starting it (background)"
minikube cp "${SCRIPT_DIR}/ebpf_monitor.py" "${REMOTE}" || { red "minikube cp failed"; exit 1; }
: > "$LOG"
minikube ssh -- "sudo python3 ${REMOTE}" > "$LOG" 2>&1 &
MON_PID=$!

# --- Step 2: let it initialize ----------------------------------------------
blue "==> Waiting 5s for monitor to initialize"
sleep 5
echo "  backend line: $(grep -m1 'backend =' "$LOG" || echo '(none yet)')"

# --- Steps 3-6: stress -> expect a CPU [ALERT] ------------------------------
blue "==> Test 1: trigger /stress, expect a CPU [ALERT]"
hit /stress
echo "  waiting 10s for detection..."
sleep 10
if grep -q "\[ALERT\] High CPU detected" "$LOG"; then
  CPU_RESULT=PASS; green "  CPU spike detected"
  grep -A5 "High CPU detected" "$LOG" | sed 's/^/    /' | head -6
else
  CPU_RESULT=FAIL; red "  no CPU alert found"
fi

# --- Steps 7-9: crash -> expect a crash [ALERT] -----------------------------
blue "==> Test 2: trigger /crash, expect 'Process crash detected'"
hit /crash
echo "  waiting 10s for detection..."
sleep 10
if grep -q "Process crash detected" "$LOG"; then
  CRASH_RESULT=PASS; green "  crash detected"
  grep -A5 "Process crash detected" "$LOG" | sed 's/^/    /' | head -6
else
  CRASH_RESULT=FAIL; red "  no crash alert found"
fi

# --- Step 11: results -------------------------------------------------------
echo
blue "===== PHASE 2 TEST RESULTS ====="
printf "CPU Spike Detection:     %s\n" "$CPU_RESULT"
printf "Process Crash Detection: %s\n" "$CRASH_RESULT"
blue "================================="

[ "$CPU_RESULT" = PASS ] && [ "$CRASH_RESULT" = PASS ]

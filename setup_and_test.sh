#!/usr/bin/env bash
#
# setup_and_test.sh — Phase 1 Victim App: build, deploy, and test all 5 endpoints.
# Target: Mac M4 (arm64) / Minikube / Docker driver / single node.
#
#   chmod +x setup_and_test.sh
#   ./setup_and_test.sh
#
# Setup steps are fatal (stop on error). Tests record PASS/FAIL and continue
# so the final summary always prints.

set -uo pipefail

APP=victim-app
IMAGE="${APP}:latest"

# CPU is reported in milli-cores; the limit is 500m. "Near 500m" = >= 300m
# (idle is ~1-5m, so this cleanly separates stressed from idle while tolerating
# throttling jitter and the Python GIL ceiling).
CPU_PASS_THRESHOLD_M=300
# Memory leak passes if usage climbs past this OR the pod is OOMKilled.
MEM_PASS_THRESHOLD_MI=200

# Test results.
HEALTH_RESULT=FAIL
STRESS_RESULT=FAIL
CRASH_RESULT=FAIL
MEMORY_RESULT=FAIL

# ----------------------------------------------------------------------------
bold() { printf "\033[1m%s\033[0m\n" "$1"; }
green(){ printf "\033[32m%s\033[0m\n" "$1"; }
red()  { printf "\033[31m%s\033[0m\n" "$1"; }
blue() { printf "\033[34m%s\033[0m\n" "$1"; }

die() { red "ERROR: $1"; exit 1; }

step() { echo; blue "==> $1"; }

# kill the background service tunnel on any exit
TUNNEL_PID=""
cleanup() {
  if [ -n "$TUNNEL_PID" ] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
    kill "$TUNNEL_PID" 2>/dev/null
  fi
}
trap cleanup EXIT

# pod name for the app's single replica
pod_name() {
  kubectl get pod -l "app=${APP}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}
# current restart count
restart_count() {
  local c
  c="$(kubectl get pod -l "app=${APP}" \
        -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null)"
  echo "${c:-0}"
}
# last-terminated reason (e.g. OOMKilled)
last_term_reason() {
  kubectl get pod -l "app=${APP}" \
    -o jsonpath='{.items[0].status.containerStatuses[0].lastState.terminated.reason}' 2>/dev/null
}
# CPU milli-cores from `kubectl top` (digits only), empty if unavailable
top_cpu_m() {
  local p; p="$(pod_name)"; [ -z "$p" ] && return 0
  kubectl top pod "$p" --no-headers 2>/dev/null | awk '{print $2}' | tr -dc '0-9'
}
# memory Mi from `kubectl top` (digits only), empty if unavailable
top_mem_mi() {
  local p; p="$(pod_name)"; [ -z "$p" ] && return 0
  kubectl top pod "$p" --no-headers 2>/dev/null | awk '{print $3}' | grep -o '^[0-9]*'
}

# ============================================================================
# SETUP (fatal on error)
# ============================================================================
bold "===== PHASE 1: SETUP ====="

command -v minikube >/dev/null || die "minikube not found in PATH"
command -v kubectl  >/dev/null || die "kubectl not found in PATH"
command -v docker   >/dev/null || die "docker not found in PATH"

step "1/6 Pointing docker at the minikube daemon"
eval "$(minikube docker-env)" || die "could not run 'minikube docker-env' (is minikube running?)"

step "2/6 Building image ${IMAGE} (arm64)"
docker build -t "${IMAGE}" . || die "docker build failed"

step "3/6 Deploying manifest"
kubectl apply -f deployment.yaml || die "kubectl apply failed"
# Ensure we pick up the freshly built image even if the deployment already existed.
kubectl rollout restart "deployment/${APP}" >/dev/null 2>&1 || true

step "4/6 Enabling metrics-server"
minikube addons enable metrics-server || die "could not enable metrics-server"

step "5/6 Waiting for the app pod to be Ready"
kubectl rollout status "deployment/${APP}" --timeout=120s || die "pod did not become Ready in time"

step "6/6 Resolving service URL"
# On the Docker driver (Mac), `minikube service --url` must keep a tunnel alive
# in the foreground, so we background it and read the URL it prints.
URL_FILE="$(mktemp)"
minikube service "${APP}" --url >"${URL_FILE}" 2>/dev/null &
TUNNEL_PID=$!
URL=""
for _ in $(seq 1 30); do
  URL="$(grep -m1 -Eo 'https?://[0-9.]+:[0-9]+' "${URL_FILE}" 2>/dev/null || true)"
  [ -n "$URL" ] && break
  sleep 1
done
rm -f "${URL_FILE}"
[ -n "$URL" ] || die "could not resolve service URL"
green "Service URL: ${URL}"

# Wait until metrics-server actually serves data (it lags ~30-60s after enable).
step "Waiting for metrics-server to start serving (up to 120s)"
METRICS_OK=0
for i in $(seq 1 24); do
  if [ -n "$(top_cpu_m)" ]; then METRICS_OK=1; green "metrics-server ready"; break; fi
  printf "\r  waiting for metrics... %ds" "$((i*5))"; sleep 5
done
echo
[ "$METRICS_OK" -eq 1 ] || red "WARNING: metrics-server not serving yet; CPU/memory checks may be unreliable."

# ============================================================================
# TESTS (never fatal)
# ============================================================================
bold "===== PHASE 1: TESTS ====="

# --- Test 1: Health ---------------------------------------------------------
step "Test 1 — Health check: GET /"
RESP="$(curl -s --max-time 10 "${URL}/" || true)"
echo "  response: ${RESP}"
case "$RESP" in
  *'"status":"healthy"'*) HEALTH_RESULT=PASS; green "  PASS" ;;
  *)                      HEALTH_RESULT=FAIL; red   "  FAIL" ;;
esac

# --- Test 2: Crash (early, while the pod is clean) ---------------------------
step "Test 2 — Crash: GET /crash (watch RESTARTS for 30s)"
BEFORE="$(restart_count)"
echo "  RESTARTS before: ${BEFORE}"
RESP="$(curl -s --max-time 10 "${URL}/crash" || true)"
echo "  response: ${RESP}"
NOW="$BEFORE"
for i in $(seq 1 30); do
  NOW="$(restart_count)"
  printf "\r  [%2ds] RESTARTS now: %s" "$i" "$NOW"
  [ "$NOW" -ge "$((BEFORE + 1))" ] && break
  sleep 1
done
echo
if [ "$NOW" -ge "$((BEFORE + 1))" ]; then
  CRASH_RESULT=PASS; green "  PASS (${BEFORE} -> ${NOW})"
else
  CRASH_RESULT=FAIL; red "  FAIL (still ${BEFORE})"
fi

# --- Recover: wait for the pod to be Running + Ready again (max 60s) ---------
step "Waiting for pod to recover after crash (max 60s)"
for i in $(seq 1 60); do
  PHASE="$(kubectl get pod -l "app=${APP}" -o jsonpath='{.items[0].status.phase}' 2>/dev/null)"
  READY="$(kubectl get pod -l "app=${APP}" -o jsonpath='{.items[0].status.containerStatuses[0].ready}' 2>/dev/null)"
  printf "\r  [%2ds] phase: %s  ready: %s   " "$i" "${PHASE:-?}" "${READY:-?}"
  if [ "$PHASE" = "Running" ] && [ "$READY" = "true" ]; then break; fi
  sleep 1
done
echo
green "  pod recovered (phase=${PHASE:-?} ready=${READY:-?})"

# --- Test 3: Stress (fresh pod; poll CPU every 5s up to 60s) -----------------
step "Test 3 — Stress: GET /stress (poll CPU every 5s, PASS at >= ${CPU_PASS_THRESHOLD_M}m)"
RESP="$(curl -s --max-time 10 "${URL}/stress" || true)"
echo "  response: ${RESP}"
STRESS_RESP_OK=0
case "$RESP" in *'"status":"stressing"'*'"threads":4'*) STRESS_RESP_OK=1 ;; esac

CPU_M=""
for i in $(seq 1 12); do
  CPU_M="$(top_cpu_m)"
  printf "\r  [%2ds] CPU: %sm (limit 500m, pass >= %sm)   " \
         "$((i*5))" "${CPU_M:-?}" "${CPU_PASS_THRESHOLD_M}"
  if [ -n "$CPU_M" ] && [ "$CPU_M" -ge "$CPU_PASS_THRESHOLD_M" ]; then break; fi
  sleep 5
done
echo
if [ "$STRESS_RESP_OK" -eq 1 ] && [ -n "$CPU_M" ] && [ "$CPU_M" -ge "$CPU_PASS_THRESHOLD_M" ]; then
  STRESS_RESULT=PASS; green "  PASS (CPU ${CPU_M}m)"
else
  STRESS_RESULT=FAIL; red "  FAIL (CPU ${CPU_M:-?}m)"
fi

# --- Test 4: Memory leak (always last; it kills the pod) ---------------------
step "Test 4 — Memory leak: GET /memory-leak (poll every 5s for 60s)"
MEM_BEFORE_RESTARTS="$(restart_count)"
RESP="$(curl -s --max-time 10 "${URL}/memory-leak" || true)"
echo "  response: ${RESP}"
for i in $(seq 1 12); do
  MEM_MI="$(top_mem_mi)"
  REASON="$(last_term_reason)"
  CUR_RESTARTS="$(restart_count)"
  printf "\r  [%2ds] mem: %sMi  lastTerm: %s  restarts: %s   " \
         "$((i*5))" "${MEM_MI:-?}" "${REASON:-none}" "${CUR_RESTARTS}"
  if [ -n "$MEM_MI" ] && [ "$MEM_MI" -gt "$MEM_PASS_THRESHOLD_MI" ]; then
    MEMORY_RESULT=PASS; break
  fi
  if [ "$REASON" = "OOMKilled" ] || [ "$CUR_RESTARTS" -gt "$MEM_BEFORE_RESTARTS" ]; then
    MEMORY_RESULT=PASS; break
  fi
  sleep 5
done
echo
if [ "$MEMORY_RESULT" = "PASS" ]; then
  green "  PASS (mem>${MEM_PASS_THRESHOLD_MI}Mi or OOMKilled)"
else
  red "  FAIL (no OOM and memory stayed <= ${MEM_PASS_THRESHOLD_MI}Mi)"
fi

# ============================================================================
# SUMMARY
# ============================================================================
echo
bold "===== PHASE 1 TEST RESULTS ====="
printf "Health Check:  %s\n" "$HEALTH_RESULT"
printf "Crash Test:    %s\n" "$CRASH_RESULT"
printf "Stress Test:   %s\n" "$STRESS_RESULT"
printf "Memory Test:   %s\n" "$MEMORY_RESULT"
bold "================================"

# Exit non-zero if any test failed.
[ "$HEALTH_RESULT" = PASS ] && [ "$STRESS_RESULT" = PASS ] && \
[ "$CRASH_RESULT" = PASS ] && [ "$MEMORY_RESULT" = PASS ]

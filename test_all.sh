#!/usr/bin/env bash
#
# test_all.sh — fully automated build + deploy + endpoint test for victim-app.
# Works on Minikube / Mac Apple Silicon / Docker driver.
#
#   chmod +x test_all.sh
#   ./test_all.sh
#
set -uo pipefail

APP=victim-app
IMAGE="${APP}:latest"
PASS_COUNT=0
FAIL_COUNT=0

green() { printf "\033[32m%s\033[0m\n" "$1"; }
red()   { printf "\033[31m%s\033[0m\n" "$1"; }
blue()  { printf "\033[34m%s\033[0m\n" "$1"; }

record() {
  # record <PASS|FAIL> <label>
  if [ "$1" = "PASS" ]; then
    green "  [PASS] $2"; PASS_COUNT=$((PASS_COUNT + 1))
  else
    red   "  [FAIL] $2"; FAIL_COUNT=$((FAIL_COUNT + 1))
  fi
}

# --- cleanup: kill the background service tunnel on any exit -------------------
TUNNEL_PID=""
cleanup() {
  if [ -n "$TUNNEL_PID" ] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
    kill "$TUNNEL_PID" 2>/dev/null
  fi
}
trap cleanup EXIT

# --- 1. point docker at minikube + build -------------------------------------
blue "==> Pointing docker at the minikube daemon"
eval "$(minikube docker-env)"

blue "==> Building image ${IMAGE}"
if ! docker build -t "${IMAGE}" .; then
  red "Docker build failed — aborting."; exit 1
fi

# --- 2. (re)deploy ------------------------------------------------------------
blue "==> Applying manifest (idempotent) and restarting rollout"
kubectl apply -f deployment.yaml >/dev/null
kubectl rollout restart "deployment/${APP}" >/dev/null

blue "==> Waiting for pod to be Ready (timeout 120s)"
if ! kubectl rollout status "deployment/${APP}" --timeout=120s; then
  red "Pod did not become Ready — aborting."; exit 1
fi

# --- 3. open service tunnel and capture URL automatically --------------------
# On the Docker driver (Mac), `minikube service --url` must keep a tunnel
# process alive in the foreground, so we run it in the background and read
# the URL it prints from a temp file.
blue "==> Opening service tunnel and resolving URL"
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

if [ -z "$URL" ]; then
  red "Could not resolve service URL — aborting."; exit 1
fi
green "Service URL: ${URL}"
echo

# --- 4. functional tests -----------------------------------------------------
blue "==> Test: GET /"
ROOT_RESP="$(curl -s --max-time 10 "${URL}/" || true)"
echo "  response: ${ROOT_RESP}"
case "$ROOT_RESP" in
  *'"status":"healthy"'*) record PASS "/ returns healthy" ;;
  *)                      record FAIL "/ returns healthy" ;;
esac
echo

blue "==> Test: GET /stress"
STRESS_RESP="$(curl -s --max-time 10 "${URL}/stress" || true)"
echo "  response: ${STRESS_RESP}"
case "$STRESS_RESP" in
  *'"status":"stressing"'*) record PASS "/stress returns stressing" ;;
  *)                        record FAIL "/stress returns stressing" ;;
esac
echo

# --- 5. crash test: RESTARTS must increase by exactly 1 ----------------------
blue "==> Test: GET /crash (watch RESTARTS for 30s)"
restarts() {
  kubectl get pod -l "app=${APP}" \
    -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null
}

BEFORE="$(restarts)"; BEFORE="${BEFORE:-0}"
echo "  RESTARTS before: ${BEFORE}"

CRASH_RESP="$(curl -s --max-time 10 "${URL}/crash" || true)"
echo "  /crash response: ${CRASH_RESP}"

CRASH_PASS=0
for i in $(seq 1 30); do
  NOW="$(restarts)"; NOW="${NOW:-0}"
  printf "\r  [%2ds] RESTARTS now: %s" "$i" "$NOW"
  if [ "$NOW" -ge "$((BEFORE + 1))" ]; then
    CRASH_PASS=1; break
  fi
  sleep 1
done
echo
if [ "$CRASH_PASS" -eq 1 ]; then
  record PASS "/crash incremented RESTARTS by >=1 (${BEFORE} -> ${NOW})"
else
  record FAIL "/crash did not increment RESTARTS within 30s (still ${BEFORE})"
fi
echo

# --- 6. summary --------------------------------------------------------------
blue "==================== SUMMARY ===================="
green "  PASS: ${PASS_COUNT}"
[ "$FAIL_COUNT" -gt 0 ] && red "  FAIL: ${FAIL_COUNT}" || echo "  FAIL: 0"
blue "================================================="

[ "$FAIL_COUNT" -eq 0 ]

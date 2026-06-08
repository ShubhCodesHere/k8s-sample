#!/usr/bin/env bash
#
# run_full_demo.sh — live Phase 2 -> 3 -> 4 self-healing demo.
# Needs NVIDIA_API_KEY and the openai package.
#
#   export NVIDIA_API_KEY=...   &&   ./run_full_demo.sh

cd "$(dirname "$0")"
REMOTE=/home/docker/ebpf_monitor.py

echo "🔥 eBPF-Swarm PROACTIVE Full Demo Starting..."
echo "Phase 2 (Snitch) → Phase 3 (Brain) → Phase 4 (Swarm)"
echo "Known patterns (cpu_spike/memory_leak) take the ⚡ FAST PATH (no LLM)."
echo "======================================================="
echo "Open 3 terminals:"
echo "  Window 1: kubectl get pods -w"
echo "  Window 2: this script"
echo "  Window 3: ./trigger_demo.sh   (fires /stress to show proactive healing)"
echo "======================================================="

# NVIDIA_API_KEY is OPTIONAL: the proactive fast path bypasses the LLM. The key is
# only needed for the LLM path (unknown/complex diagnoses).
if [ -z "${NVIDIA_API_KEY:-}" ]; then
  echo "NOTE: NVIDIA_API_KEY not set — LLM path disabled; proactive fast path still works."
fi

minikube cp ./ebpf_monitor.py "${REMOTE}" 2>/dev/null
# Note: swarm.py reads diagnoses.log directly (written by causal_engine.py), so
# it does not need to be in the pipe. We background the Snitch->Brain pipe and run
# the swarm in the foreground so its agent chatter is what you watch.
( minikube ssh -- "sudo python3 ${REMOTE}" | python3 -u causal_engine.py ) &
PIPE_PID=$!
trap 'kill "$PIPE_PID" 2>/dev/null; minikube ssh -- "sudo pkill -f ebpf_monitor.py" 2>/dev/null' EXIT
python3 swarm.py

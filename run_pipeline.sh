#!/usr/bin/env bash
#
# run_pipeline.sh — live Phase 2 -> Phase 3 demo.
# Streams the in-node Snitch through the Brain so diagnoses print as events happen.
#
#   chmod +x run_pipeline.sh
#   ./run_pipeline.sh
#
# In another terminal, trigger events, e.g.:
#   POD_IP=$(kubectl get pod -l app=victim-app -o jsonpath='{.items[0].status.podIP}')
#   minikube ssh -- curl -s http://$POD_IP:8000/stress
#   minikube ssh -- curl -s http://$POD_IP:8000/crash

cd "$(dirname "$0")"
REMOTE=/home/docker/ebpf_monitor.py

echo "Starting eBPF-Swarm Pipeline..."
echo "Phase 2 (Snitch) -> Phase 3 (Brain)"
echo "Trigger events with: curl <URL>/stress or curl <URL>/crash"
echo "Press Ctrl+C to stop"
echo "========================================"

# Make sure the monitor is present on the node, then stream it through the Brain.
minikube cp ./ebpf_monitor.py "${REMOTE}" 2>/dev/null
minikube ssh -- "sudo python3 ${REMOTE}" | python3 -u causal_engine.py

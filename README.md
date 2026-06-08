# eBPF-Swarm

A proactive, self-healing Kubernetes system: detect resource trouble from kernel/`/proc`
telemetry and restart the affected pod **before** it crashes — with an AI agent swarm for
the unknown cases and a rule-based **fast path** for known ones.

Built and tested on **Minikube (Docker driver) on macOS / Apple Silicon**, single node.

## Architecture

```
Phase 1  Victim App      FastAPI pod with breakable endpoints (/stress, /memory-leak, /crash)
Phase 2  Snitch          ebpf_monitor.py — runs in the node, watches CPU/memory vs each
                         pod's cgroup limit + process exits, emits WARNING/CRITICAL alerts
Phase 3  Brain           causal_engine.py — parses alerts -> JSON diagnoses (metric, urgency,
                         trend, severity), maps to a pod
Phase 4  Swarm           swarm.py — Planner -> Evaluator -> Executor agents (NVIDIA Nemotron).
                         Known patterns (cpu_spike/memory_leak) take a ⚡ fast path that
                         bypasses the LLM and restarts the pod in ~2s.
```

## Components

| File | Role |
|------|------|
| `main.py`, `Dockerfile`, `deployment.yaml` | Phase 1 victim app |
| `ebpf_monitor.py` | Phase 2 telemetry (bcc if available, else `/proc`+cgroup polling) |
| `causal_engine.py` | Phase 3 diagnosis engine (stdlib only) |
| `swarm.py` | Phase 4 agent swarm + fast path |
| `setup_and_test.sh` / `test_phase{2,3,4}.sh` | per-phase automated tests |
| `run_full_demo.sh` / `trigger_demo.sh` | live end-to-end demo |

## Quick start

```bash
# Phase 1: build + deploy the victim app
eval $(minikube docker-env)
docker build -t victim-app:latest .
kubectl apply -f deployment.yaml
minikube addons enable metrics-server

# Phase 4: proactive healing test (no API key needed — fast path bypasses the LLM)
./test_phase4.sh
```

For the LLM path (unknown/complex diagnoses):

```bash
pip3 install openai
export NVIDIA_API_KEY=...        # NVIDIA NIM key (integrate.api.nvidia.com)
# export NVIDIA_MODEL=...        # override if the default model id 404s
```

## Design notes / known limitations

- **CPU/memory are measured relative to each pod's own cgroup limit**, not absolute cores/MiB
  — a 500m-limited pod throttles its workers to a fraction of a core, so "% of limit" is the
  real saturation signal and avoids false alarms from large unrelated pods.
- **eBPF falls back to `/proc` polling.** Minikube's `linuxkit` kernel ships no matching
  `linux-headers`, so bcc can't compile; the `/proc`+cgroup path is what actually runs.
  Trade-off: process exit codes are unavailable in fallback mode.
- **Restart = `kubectl delete pod --grace-period=0 --force`.** A graceful delete blocks while a
  CPU-saturated pod fails to handle SIGTERM. Heal is verified by the pod name changing to a
  fresh Running pod (delete makes a new pod; restartCount resets to 0).
- **A CPU spike throttles, it does not crash** the victim — so "proactive" there means
  "restart a saturated pod." The genuine before-OOM win is `memory_leak`, but the leak can
  hit the limit within one poll, so that catch is best-effort.
- Pod attribution in Phase 3 is a name-match heuristic; for multi-app clusters, attribute by
  PID → cgroup → container ID instead.

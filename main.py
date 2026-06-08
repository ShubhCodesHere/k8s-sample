import multiprocessing
import os
import threading

from fastapi import FastAPI

app = FastAPI(title="Victim App")

# Holds leaked memory so it is never garbage-collected.
_leak = []


def _burn_cpu():
    """Tight infinite loop — pins one core at 100%."""
    x = 0
    while True:
        x += 1
        x *= 2
        x %= 1_000_000_007


@app.get("/")
def root():
    return {"status": "healthy"}


@app.get("/stress")
def stress():
    """Spawn 4 daemon processes to peg multiple cores.

    Processes (not threads) are required: the GIL serializes CPU-bound Python
    threads onto a single core, so threads can't drive usage to the 500m cgroup
    limit. Four real processes generate >4 cores of demand, which K8s throttles
    down to the 500m limit -- visible as ~500m in `kubectl top pods`.
    """
    for _ in range(4):
        p = multiprocessing.Process(target=_burn_cpu, daemon=True)
        p.start()
    return {"status": "stressing", "threads": 4}


@app.get("/memory-leak")
def memory_leak():
    """Grow memory in a background thread until the pod is OOMKilled."""

    def _leaker():
        # ~10 MB per append.
        chunk = "x" * (10 * 1024 * 1024)
        while True:
            _leak.append(chunk * 1)

    t = threading.Thread(target=_leaker, daemon=True)
    t.start()
    return {"status": "leaking"}


@app.get("/recover")
def recover():
    return {"status": "recovered"}


@app.get("/crash")
def crash():
    def kill():
        import time
        time.sleep(0.1)
        # uvicorn is PID 1 in the container, and the kernel ignores unhandled
        # fatal SIGNALS sent to PID 1 from its own namespace (init protection).
        # os._exit is a voluntary exit syscall, not a signal, so it bypasses
        # that protection: PID 1 exits -> container dies -> K8s restarts the pod.
        os._exit(1)
    t = threading.Thread(target=kill)
    t.daemon = False
    t.start()
    return {"status": "crashing"}
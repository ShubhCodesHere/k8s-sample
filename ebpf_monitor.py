#!/usr/bin/env python3
"""
eBPF-Swarm Phase 2 — The Snitch (telemetry monitor).

Runs INSIDE the Minikube node (Linux). Detects, within ~1s:
  * High CPU: a pod using > THRESHOLD of its cgroup CPU limit (throttling).
  * Crashes: a watched pod process disappearing.

Two backends:
  1. bcc/eBPF  — used only if it actually compiles+loads (gives true exit codes
                 via the sched_process_exit tracepoint).
  2. /proc poll — the robust fallback. On Minikube's Docker driver the running
                 kernel is a linuxkit kernel with no matching linux-headers, so
                 bcc cannot compile and THIS path is what really runs.

CPU is measured against each pod's cgroup CPU *limit* (cpu.max), not against a
single core: a 500m-limited pod throttles its workers to a fraction of a core,
so "80% of one core" can never fire — "80% of the pod's limit" is the real
saturation signal. See the README/notes for why.
"""

import os
import sys
import time
from datetime import datetime

# ---- tunables ---------------------------------------------------------------
POLL_SECONDS = 1.0
# Proactive thresholds, as a fraction of each pod's cgroup CPU limit.
CPU_WARN_FRACTION = 0.60              # WARNING: climbing
CPU_CRIT_FRACTION = 0.85             # CRITICAL: near limit
# Memory thresholds as a fraction of each pod's OWN memory limit. ~60%/85% of a
# 256Mi limit == ~154Mi/218Mi, matching the spec's 150Mi/220Mi intent — but
# relative, so a big system pod near nobody's limit doesn't false-alarm.
MEM_WARN_FRACTION = 0.60             # WARNING: climbing
MEM_CRIT_FRACTION = 0.85            # CRITICAL: near limit
ALERT_COOLDOWN_SECONDS = 10           # don't repeat an alert for same cgroup+kind
STABLE_CYCLES_BEFORE_WATCH = 2        # ignore very short-lived procs for crashes
CGROUP_ROOT = "/sys/fs/cgroup"
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def emit_cpu_warning(name, pid, cpu_pct, trend):
    print("[WARNING] CPU climbing!", flush=True)
    print(f"Process: {name}", flush=True)
    print(f"PID: {pid}", flush=True)
    print(f"CPU%: {cpu_pct:.1f}", flush=True)
    print(f"Trend: {trend}", flush=True)
    print(f"Time: {ts()}", flush=True)
    print("Action needed: preemptive restart recommended", flush=True)
    print("", flush=True)


def emit_cpu_critical(name, pid, cpu_pct, trend):
    print("[CRITICAL] CPU near limit!", flush=True)
    print(f"Process: {name}", flush=True)
    print(f"PID: {pid}", flush=True)
    print(f"CPU%: {cpu_pct:.1f}", flush=True)
    print(f"Trend: {trend}", flush=True)
    print(f"Time: {ts()}", flush=True)
    print("Action needed: immediate restart required", flush=True)
    print("", flush=True)


def emit_mem_warning(name, pid, mem_mi, trend):
    print("[WARNING] Memory climbing!", flush=True)
    print(f"Process: {name}", flush=True)
    print(f"PID: {pid}", flush=True)
    print(f"Memory: {mem_mi:.0f}Mi", flush=True)
    print(f"Trend: {trend}", flush=True)
    print(f"Time: {ts()}", flush=True)
    print("Action needed: preemptive restart recommended", flush=True)
    print("", flush=True)


def emit_mem_critical(name, pid, mem_mi, trend):
    print("[CRITICAL] Memory near limit!", flush=True)
    print(f"Process: {name}", flush=True)
    print(f"PID: {pid}", flush=True)
    print(f"Memory: {mem_mi:.0f}Mi", flush=True)
    print(f"Trend: {trend}", flush=True)
    print(f"Time: {ts()}", flush=True)
    print("Action needed: immediate restart required", flush=True)
    print("", flush=True)


def trend_of(history):
    """Classify the trend from recent samples (oldest..newest)."""
    if len(history) >= 2:
        if history[-1] > history[-2] * 1.05:
            return "rising"
        if history[-1] < history[-2] * 0.95:
            return "falling"
    return "stable"


def emit_crash_alert(name, pid, exit_code):
    print("[ALERT] Process crash detected!", flush=True)
    print(f"Process: {name}", flush=True)
    print(f"PID: {pid}", flush=True)
    print(f"Exit code: {exit_code}", flush=True)
    print(f"Time: {ts()}", flush=True)
    print("Action needed: pod may need restart", flush=True)
    print("", flush=True)


# ----------------------------------------------------------------------------
# /proc + cgroup helpers
# ----------------------------------------------------------------------------
def list_pids():
    return [int(d) for d in os.listdir("/proc") if d.isdigit()]


def read_comm(pid):
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return "?"


def read_proc_cpu_ticks(pid):
    """utime+stime in clock ticks for a process, or None if gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except OSError:
        return None
    # comm is in parens and may contain spaces/parens -> split on the LAST ')'
    rparen = data.rfind(")")
    fields = data[rparen + 2:].split()
    # after comm, fields[11]=utime, fields[12]=stime (0-indexed from 'state')
    try:
        return int(fields[11]) + int(fields[12])
    except (IndexError, ValueError):
        return None


def read_cgroup_rel(pid):
    """cgroup v2 relative path for a pid (the part after '0::'), or None."""
    try:
        with open(f"/proc/{pid}/cgroup") as f:
            for line in f:
                # v2 unified line looks like: "0::/kubepods.slice/.../scope"
                if line.startswith("0::"):
                    return line.strip().split("::", 1)[1]
    except OSError:
        pass
    return None


def cgroup_cpu_limit_cores(rel):
    """Walk up the cgroup tree from `rel` until a numeric cpu.max is found.
    Returns cores allowed (quota/period) or None if unlimited/unknown."""
    parts = rel.strip("/").split("/") if rel and rel != "/" else []
    for i in range(len(parts), -1, -1):
        sub = "/".join(parts[:i])
        path = os.path.join(CGROUP_ROOT, sub, "cpu.max") if sub else os.path.join(CGROUP_ROOT, "cpu.max")
        try:
            with open(path) as f:
                quota, period = f.read().split()
            if quota != "max":
                return float(quota) / float(period)
        except (OSError, ValueError):
            continue
    return None


def cgroup_mem_current_mi(rel):
    """Current memory usage (MiB) of a cgroup, or None."""
    if not rel:
        return None
    path = os.path.join(CGROUP_ROOT, rel.strip("/"), "memory.current")
    try:
        with open(path) as f:
            return int(f.read().strip()) / (1024 * 1024)
    except (OSError, ValueError):
        return None


def cgroup_mem_limit_mi(rel):
    """Walk up the cgroup tree until a numeric memory.max is found.
    Returns the limit in MiB, or None if unlimited/unknown."""
    parts = rel.strip("/").split("/") if rel and rel != "/" else []
    for i in range(len(parts), -1, -1):
        sub = "/".join(parts[:i])
        path = os.path.join(CGROUP_ROOT, sub, "memory.max") if sub else os.path.join(CGROUP_ROOT, "memory.max")
        try:
            with open(path) as f:
                val = f.read().strip()
            if val != "max":
                return int(val) / (1024 * 1024)
        except (OSError, ValueError):
            continue
    return None


def is_pod_cgroup(rel):
    return rel is not None and "kubepods" in rel


# ----------------------------------------------------------------------------
# bcc backend (best-effort; expected to fail on linuxkit kernels)
# ----------------------------------------------------------------------------
def try_init_bcc():
    """Return a BPF object attached to sched_process_exit, or None on any failure."""
    try:
        # pyrefly: ignore [missing-import]
        from bcc import BPF
    except Exception:
        return None
    prog = r"""
    #include <linux/sched.h>
    struct data_t { u32 pid; int code; char comm[16]; };
    BPF_PERF_OUTPUT(events);
    TRACEPOINT_PROBE(sched, sched_process_exit) {
        struct data_t d = {};
        struct task_struct *t = (struct task_struct *)bpf_get_current_task();
        d.pid = bpf_get_current_pid_tgid() >> 32;
        d.code = t->exit_code >> 8;      // exit() status
        bpf_get_current_comm(&d.comm, sizeof(d.comm));
        events.perf_submit(args, &d, sizeof(d));
        return 0;
    }
    """
    try:
        return BPF(text=prog)        # compiles -> needs kernel headers; fails here
    except Exception as e:
        print(f"[info] bcc/eBPF unavailable ({e.__class__.__name__}); using /proc fallback.", flush=True)
        return None


# ----------------------------------------------------------------------------
# main loop
# ----------------------------------------------------------------------------
def main():
    bpf = try_init_bcc()
    backend = "eBPF (bcc)" if bpf else "/proc polling (fallback)"
    print(f"[eBPF-Swarm Snitch] backend = {backend}", flush=True)
    print(f"[eBPF-Swarm Snitch] PROACTIVE mode: WARNING>{int(CPU_WARN_FRACTION*100)}% / CRITICAL>{int(CPU_CRIT_FRACTION*100)}% "
          f"of each pod's CPU+MEM limit; poll {POLL_SECONDS}s", flush=True)
    print("[eBPF-Swarm Snitch] Ctrl+C to stop.", flush=True)
    print("", flush=True)

    # crash events captured by the bcc tracepoint (pid -> exit_code)
    bcc_exits = {}
    if bpf:
        def _on_exit(cpu, data, size):
            ev = bpf["events"].event(data)
            bcc_exits[ev.pid] = (ev.comm.decode("utf-8", "replace"), ev.code)
        bpf["events"].open_perf_buffer(_on_exit)

    prev_ticks = {}            # pid -> cpu ticks last cycle
    prev_meta = {}             # pid -> (comm, cgroup_rel, seen_count)
    cg_last_alert = {}         # (cgroup_rel, kind) -> last alert monotonic time
    cg_cpu_hist = {}           # cgroup_rel -> [recent cpu fractions]
    cg_mem_hist = {}           # cgroup_rel -> [recent memory MiB]
    prev_loop_start = None     # monotonic time of the previous cycle

    while True:
        loop_start = time.monotonic()
        if bpf:
            bpf.perf_buffer_poll(timeout=0)

        pids = list_pids()
        cur_ticks = {}
        cur_meta = {}
        # per-cgroup aggregation for this cycle
        cg_proc_ticks = {}     # cgroup_rel -> {pid: delta_ticks}
        cg_limit = {}          # cgroup_rel -> cores

        for pid in pids:
            t = read_proc_cpu_ticks(pid)
            if t is None:
                continue
            comm = read_comm(pid)
            rel = read_cgroup_rel(pid)
            cur_ticks[pid] = t
            seen = prev_meta.get(pid, (None, None, 0))[2] + 1
            cur_meta[pid] = (comm, rel, seen)

            if is_pod_cgroup(rel) and pid in prev_ticks:
                delta = t - prev_ticks[pid]
                if delta < 0:
                    delta = 0
                cg_proc_ticks.setdefault(rel, {})[pid] = delta
                if rel not in cg_limit:
                    cg_limit[rel] = cgroup_cpu_limit_cores(rel)

        # actual elapsed time since the previous cycle's tick reads
        dt = (loop_start - prev_loop_start) if prev_loop_start else POLL_SECONDS
        dt = max(dt, 1e-6)
        prev_loop_start = loop_start

        # ---- proactive CPU + memory detection (per pod cgroup) ----
        now = time.monotonic()

        def cooled(rel, kind):
            return now - cg_last_alert.get((rel, kind), 0) >= ALERT_COOLDOWN_SECONDS

        for rel, procs in cg_proc_ticks.items():
            top_pid = max(procs, key=procs.get)
            name = cur_meta.get(top_pid, ("?",))[0]

            # CPU vs the pod's cgroup limit
            cores_limit = cg_limit.get(rel)
            if cores_limit:
                used_cores = (sum(procs.values()) / CLK_TCK) / dt
                frac = used_cores / cores_limit
                hist = cg_cpu_hist.setdefault(rel, [])
                hist.append(frac)
                del hist[:-3]
                trend = trend_of(hist)
                if frac >= CPU_CRIT_FRACTION and cooled(rel, "cpu"):
                    cg_last_alert[(rel, "cpu")] = now
                    emit_cpu_critical(name, top_pid, frac * 100.0,
                                      "rising fast" if trend == "rising" else trend)
                elif frac >= CPU_WARN_FRACTION and cooled(rel, "cpu"):
                    cg_last_alert[(rel, "cpu")] = now
                    emit_cpu_warning(name, top_pid, frac * 100.0, trend)

            # Memory as a fraction of the pod's OWN limit (skip unlimited pods).
            mem_mi = cgroup_mem_current_mi(rel)
            mem_limit = cgroup_mem_limit_mi(rel)
            if mem_mi is not None and mem_limit:
                mfrac = mem_mi / mem_limit
                mhist = cg_mem_hist.setdefault(rel, [])
                mhist.append(mem_mi)
                del mhist[:-3]
                mtrend = trend_of(mhist)
                if mfrac >= MEM_CRIT_FRACTION and cooled(rel, "mem"):
                    cg_last_alert[(rel, "mem")] = now
                    emit_mem_critical(name, top_pid, mem_mi,
                                      "rising fast" if mtrend == "rising" else mtrend)
                elif mfrac >= MEM_WARN_FRACTION and cooled(rel, "mem"):
                    cg_last_alert[(rel, "mem")] = now
                    emit_mem_warning(name, top_pid, mem_mi, mtrend)

        # ---- crash detection (watched pid disappeared) ----
        vanished_by_cg = {}
        for pid, (comm, rel, seen) in prev_meta.items():
            if pid in cur_ticks:
                continue
            # only care about stable pod workload processes
            if seen < STABLE_CYCLES_BEFORE_WATCH or not is_pod_cgroup(rel):
                continue
            vanished_by_cg.setdefault(rel, []).append((pid, comm))
        for rel, dead in vanished_by_cg.items():
            pid, comm = sorted(dead)[0]                  # one alert per pod
            if pid in bcc_exits:
                comm, code = bcc_exits.pop(pid)
            else:
                code = "unknown (proc-poll cannot read exit code)"
            emit_crash_alert(comm, pid, code)

        prev_ticks = cur_ticks
        prev_meta = cur_meta

        sleep_left = POLL_SECONDS - (time.monotonic() - loop_start)
        if sleep_left > 0:
            time.sleep(sleep_left)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[eBPF-Swarm Snitch] stopped.", flush=True)
        sys.exit(0)

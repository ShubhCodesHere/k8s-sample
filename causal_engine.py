#!/usr/bin/env python3
"""
eBPF-Swarm Phase 3 — The Brain (Causal Engine).

Reads the Snitch's alert stream on stdin (pipe it from Phase 2), parses each
[ALERT] block, maps it to a Kubernetes pod, and emits a JSON diagnosis to stdout
(and appends it to diagnoses.log). Stdlib only.

Block parsing note: the Phase 2 monitor separates alert blocks with a BLANK LINE
(it does not print '---'). So a block is closed on any of: a blank line, a '---'
line, the start of the next '[ALERT]', or EOF. This accepts both the real stream
and the '---'-separated form shown in the spec.
"""

import json
import re
import subprocess
import sys
from datetime import datetime

DIAGNOSES_FILE = "diagnoses.log"
SEPARATOR = "[Brain] Diagnosis complete. Waiting for next alert..."

ALERT_TAGS = ("[ALERT]", "[WARNING]", "[CRITICAL]")

# process name -> substring of the owning pod's name (best-effort mapping)
PROCESS_TO_POD_HINT = {"uvicorn": "victim-app"}


def classify(title):
    """Map an alert title to metric/action/urgency/confidence/severity.

    Levels: [WARNING] -> preemptive (proactive), [CRITICAL]/[ALERT] -> immediate,
    process crash -> post-mortem (reactive).
    """
    t = title.lower()
    if "crash" in t:
        return dict(metric="process_crash", action="restart_pod",
                    urgency="post-mortem", confidence="99%", severity="critical")

    metric = "memory_leak" if "memory" in t else "cpu_spike" if "cpu" in t else "unknown"
    action = "restart_pod" if metric in ("cpu_spike", "memory_leak") else "investigate"

    if "[warning]" in t:
        urgency, confidence, severity = "preemptive", "85%", "warning"
    else:  # [CRITICAL], legacy [ALERT] High CPU/memory, etc.
        urgency, confidence, severity = "immediate", "99%", "critical"
    return dict(metric=metric, action=action,
                urgency=urgency, confidence=confidence, severity=severity)


def to_iso(raw):
    """'2026-06-08 00:15:42' -> '2026-06-08T00:15:42'; tolerate junk."""
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).isoformat()
        except ValueError:
            continue
    return raw or datetime.now().isoformat(timespec="seconds")


def lookup_pod(process_name):
    """Find the Running pod that owns this process. Falls back gracefully."""
    try:
        result = subprocess.run(
            ["kubectl", "get", "pods", "--no-headers", "-o",
             "custom-columns=NAME:.metadata.name,STATUS:.status.phase"],
            capture_output=True, text=True, timeout=10,
        )
        rows = [ln.split() for ln in result.stdout.splitlines() if ln.strip()]
        running = [(r[0], r[1]) for r in rows if len(r) >= 2 and r[1] == "Running"]
        if not running:
            return "victim-app-unknown"
        hint = PROCESS_TO_POD_HINT.get(process_name, process_name)
        for name, _ in running:
            if hint and hint in name:
                return name
        return running[0][0]            # some Running pod, but not matched
    except Exception:
        return "victim-app-unknown"


def parse_trend(text):
    m = re.search(r"Trend:\s*(.+)", text)
    if not m:
        return "stable"
    val = m.group(1).strip().lower()
    if "rising" in val:        # normalizes "rising fast" -> "rising"
        return "rising"
    if "falling" in val:
        return "falling"
    return "stable"


def parse_block(lines):
    """Turn an alert block (list of lines) into a diagnosis dict, or None."""
    title = lines[0] if lines else ""
    c = classify(title)

    text = "\n".join(lines)
    proc_m = re.search(r"Process:\s*(.+)", text)
    pid_m = re.search(r"PID:\s*(\d+)", text)
    cpu_m = re.search(r"CPU%:\s*([\d.]+)", text)
    mem_m = re.search(r"Memory:\s*([\d.]+)", text)
    exit_m = re.search(r"Exit code:\s*(.+)", text)
    time_m = re.search(r"Time:\s*(.+)", text)

    process = proc_m.group(1).strip() if proc_m else "unknown"
    pid = int(pid_m.group(1)) if pid_m else None
    cpu_percent = float(cpu_m.group(1)) if cpu_m else None
    memory_mi = float(mem_m.group(1)) if mem_m else None
    timestamp = to_iso(time_m.group(1) if time_m else "")
    trend = parse_trend(text)

    # field order follows the Phase-4 spec example
    diagnosis = {
        "root_cause": lookup_pod(process),
        "metric": c["metric"],
        "urgency": c["urgency"],
        "confidence": c["confidence"],
        "process": process,
        "pid": pid,
    }
    if cpu_percent is not None:
        diagnosis["cpu_percent"] = cpu_percent
    if memory_mi is not None:
        diagnosis["memory_mi"] = memory_mi
    if c["metric"] == "process_crash":
        diagnosis["exit_code"] = exit_m.group(1).strip() if exit_m else "unknown"
    diagnosis.update({
        "trend": trend,
        "recommended_action": c["action"],
        "severity": c["severity"],
        "namespace": "default",
        "timestamp": timestamp,
    })
    return diagnosis


def handle_block(lines):
    if not lines:
        return
    try:
        diagnosis = parse_block(lines)
    except Exception as e:  # never crash on bad input
        print(f"[Brain] skipped malformed alert ({e.__class__.__name__})", flush=True)
        return
    line = json.dumps(diagnosis)
    print(line, flush=True)
    try:
        with open(DIAGNOSES_FILE, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[Brain] could not write {DIAGNOSES_FILE}: {e}", flush=True)
    print(SEPARATOR, flush=True)


def main():
    block = []
    in_block = False
    # readline() so we react per-line even when stdin is a pipe
    for raw in iter(sys.stdin.readline, ""):
        line = raw.rstrip("\n")
        if any(tag in line for tag in ALERT_TAGS):
            if in_block:
                handle_block(block)
            block = [line]
            in_block = True
        elif in_block:
            if line.strip() in ("", "---"):
                handle_block(block)
                block, in_block = [], False
            else:
                block.append(line)
    if in_block:
        handle_block(block)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Brain] stopped.", flush=True)
        sys.exit(0)

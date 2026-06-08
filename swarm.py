#!/usr/bin/env python3
"""
eBPF-Swarm Phase 4 — The AI Swarm (Planner -> Evaluator -> Executor).

Tails diagnoses.log (Phase 3 output). For each fresh, critical diagnosis it runs
three agents in sequence:
  1. Planner   (LLM)  -> decides the kubectl action
  2. Evaluator (LLM)  -> approves/denies the action
  3. Executor  (kubectl) -> runs the action, then verifies recovery

Design notes (see README / phase-4 notes):
  * Clock skew: the Snitch stamps node-local time (UTC here) but this runs on the
    Mac (IST). Freshness is computed as the MIN absolute age across both local-
    and UTC-interpretations, so a genuinely-new diagnosis is never dropped as
    "stale" just because the two machines disagree on timezone.
  * restart_pod = `kubectl delete pod`, which creates a NEW pod (restartCount
    resets to 0). Recovery is verified by a Running pod existing for the label,
    not by a restartCount bump.
  * Hardcoded safety: system namespaces are never touched, regardless of what the
    LLM approves.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

# ---- config ----------------------------------------------------------------
BASE_URL = "https://integrate.api.nvidia.com/v1"
MODEL = os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
DIAGNOSES_FILE = os.environ.get("DIAGNOSES_FILE", "diagnoses.log")
FRESH_SECONDS = 60
KUBECTL_TIMEOUT = 30
COOLDOWN_SECONDS = 120             # don't re-heal the same pod within this window
SYSTEM_NAMESPACES = {"kube-system", "kube-public", "kube-node-lease"}

# Known patterns that skip the LLM entirely and go straight to the Executor.
FAST_PATH_RULES = {
    "cpu_spike": {
        "urgency": ["preemptive", "immediate"],
        "action": "restart_pod",
        "bypass_llm": True,
        "max_response_time_seconds": 5,
    },
    "memory_leak": {
        "urgency": ["preemptive", "immediate"],
        "action": "restart_pod",
        "bypass_llm": True,
        "max_response_time_seconds": 5,
    },
}
last_action = {}  # pod_name -> monotonic time of last heal

PLANNER_SYS = """You are a Senior SRE engineer managing a Kubernetes cluster.
You will receive a JSON diagnosis of a problem.
Your job is to decide the single best Kubernetes action to fix it.
Reply with ONLY a JSON object in this exact format, nothing else:
{
  "action": "restart_pod",
  "target": "<pod_name>",
  "namespace": "<namespace>",
  "reason": "<one sentence explanation>",
  "urgency": "immediate"
}
Valid actions are: restart_pod, cordon_node, scale_down, rollback"""

EVALUATOR_SYS = """You are a Kubernetes Security Auditor.
You will receive a proposed action from a Planner agent.
Your job is to evaluate if this action is safe to execute automatically.
Reply with ONLY a JSON object in this exact format, nothing else:
{
  "approved": true,
  "risk_level": "low",
  "reason": "<one sentence explanation>"
}
Only approve if: action is restart_pod or scale_down AND urgency is immediate AND target is not a system namespace (kube-system, kube-public)"""

STRICTER = "\nReturn ONLY raw JSON. No markdown fences, no commentary, no prose."


# ---- LLM plumbing ----------------------------------------------------------
_client = None


def get_client():
    global _client
    if _client is None:
        from openai import OpenAI  # lazy: file imports without openai installed
        key = os.environ.get("NVIDIA_API_KEY")
        if not key:
            raise RuntimeError("NVIDIA_API_KEY is not set")
        _client = OpenAI(base_url=BASE_URL, api_key=key)
    return _client


def _raw_llm(system_prompt, user_prompt):
    resp = get_client().chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": system_prompt},
                  {"role": "user", "content": user_prompt}],
        temperature=0.1,
        max_tokens=512,
        stream=False,
    )
    return resp.choices[0].message.content or ""


def extract_json(text):
    """Pull the first JSON object out of an LLM reply (tolerates fences/prose)."""
    if not text:
        return None
    try:
        return json.loads(text.strip())
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)  # first {...} blob
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def llm_json(system_prompt, user_prompt):
    """Call the LLM, parse JSON; retry once with a stricter prompt; else None."""
    try:
        out = extract_json(_raw_llm(system_prompt, user_prompt))
        if out is not None:
            return out
    except Exception as e:
        print(f"[Swarm] LLM call failed: {e.__class__.__name__}: {e}", flush=True)
    # one retry, stricter
    try:
        out = extract_json(_raw_llm(system_prompt + STRICTER, user_prompt))
        return out
    except Exception as e:
        print(f"[Swarm] LLM retry failed: {e.__class__.__name__}: {e}", flush=True)
        return None


# ---- agents ----------------------------------------------------------------
def planner(diagnosis):
    decision = llm_json(PLANNER_SYS, "Here is the diagnosis: " + json.dumps(diagnosis))
    if not decision or "action" not in decision:
        print("[Planner] could not produce a valid decision; skipping.", flush=True)
        return None
    print(f"[Planner] Decision: {decision.get('action')} on {decision.get('target')}", flush=True)
    print(f"[Planner] Reason: {decision.get('reason', '')}", flush=True)
    return decision


def evaluator(decision):
    verdict = llm_json(EVALUATOR_SYS, "The Planner wants to execute: " + json.dumps(decision))
    if not verdict:
        print("[Evaluator] BLOCKED: no valid verdict from auditor", flush=True)
        return False
    approved = bool(verdict.get("approved"))
    print(f"[Evaluator] Risk level: {verdict.get('risk_level', 'unknown')}", flush=True)
    print(f"[Evaluator] Approved: {approved}", flush=True)
    print(f"[Evaluator] Reason: {verdict.get('reason', '')}", flush=True)
    if not approved:
        print(f"[Evaluator] BLOCKED: {verdict.get('reason', 'not approved')}", flush=True)
        return False
    return True


def _kubectl(args):
    return subprocess.run(["kubectl"] + args, capture_output=True, text=True,
                          timeout=KUBECTL_TIMEOUT)


def executor(decision):
    """Run the action. Returns (success, timing) where timing has monotonic
    marks: fired, deleted, running (or None)."""
    timing = {"fired": None, "deleted": None, "running": None}
    action = decision.get("action")
    target = decision.get("target")
    namespace = decision.get("namespace") or "default"

    # Hardcoded safety net — independent of the LLM's approval.
    if namespace in SYSTEM_NAMESPACES:
        print(f"[Executor] SAFETY BLOCK: refusing to act on system namespace '{namespace}'", flush=True)
        return False, timing
    if not target:
        print("[Executor] no target specified; aborting.", flush=True)
        return False, timing

    if action == "restart_pod":
        # Force/grace-0: a saturated pod can't process SIGTERM fast enough, so a
        # graceful delete blocks for the whole grace period. For a "restart NOW"
        # heal we evict immediately; the ReplicaSet recreates the pod at once.
        cmd = ["delete", "pod", target, "-n", namespace,
               "--grace-period=0", "--force"]
    elif action == "scale_down":
        cmd = ["scale", "deployment", target, "--replicas=0", "-n", namespace]
    else:
        print(f"[Executor] unsupported action '{action}'; aborting.", flush=True)
        return False, timing

    timing["fired"] = time.monotonic()
    print(f"[Executor] Executing: kubectl {' '.join(cmd)}", flush=True)
    try:
        res = _kubectl(cmd)
    except subprocess.TimeoutExpired:
        print("[Executor] kubectl timed out.", flush=True)
        return False, timing
    timing["deleted"] = time.monotonic()
    out = (res.stdout or res.stderr).strip()
    print(f"[Executor] Command output: {out}", flush=True)
    if res.returncode != 0:
        print("[Executor] command failed; no recovery to verify.", flush=True)
        return False, timing
    print(f"[Executor] Pod restarted in {timing['deleted'] - timing['fired']:.1f}s", flush=True)
    print("[Executor] Action complete. Kubernetes will recreate the pod.", flush=True)

    if action == "scale_down":
        return True, timing

    # poll for the fresh pod to be Running (accurate timing, no fixed sleep)
    for _ in range(25):
        try:
            chk = _kubectl(["get", "pods", "-l", "app=victim-app", "-n", namespace,
                            "--no-headers", "-o",
                            "custom-columns=NAME:.metadata.name,STATUS:.status.phase"])
        except subprocess.TimeoutExpired:
            break
        if any(ln.strip().endswith("Running") for ln in chk.stdout.splitlines()):
            timing["running"] = time.monotonic()
            print("[Executor] Verification: pod is Running again ✓", flush=True)
            return True, timing
        time.sleep(1)
    print("[Executor] Verification: pod not Running yet.", flush=True)
    return False, timing


# ---- freshness (skew-tolerant) --------------------------------------------
def compute_age(ts_str):
    """Seconds since the alert was generated. Robust to Mac/node timezone
    disagreement: smallest |age| across local and UTC interpretations."""
    if not ts_str:
        return 0.0
    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        return 0.0
    if ts.tzinfo is not None:
        return abs((datetime.now(timezone.utc) - ts).total_seconds())
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    return min(abs((datetime.now() - ts).total_seconds()),
               abs((now_utc_naive - ts).total_seconds()))


def is_fresh(ts_str):
    if not ts_str:
        return True
    return compute_age(ts_str) <= FRESH_SECONDS


# ---- timing proof ----------------------------------------------------------
def print_timing(diagnosis, received_dt, timing):
    """Print the [Timer] block. received_dt is wall-clock when we read the line;
    timing carries monotonic marks from the executor."""
    def hhmmss(dt):
        return dt.strftime("%H:%M:%S")

    age = compute_age(diagnosis.get("timestamp"))
    alert_dt = received_dt - timedelta(seconds=age)         # on the local timeline
    fired = timing.get("fired")
    deleted = timing.get("deleted")
    running = timing.get("running")
    # convert monotonic marks to wall-clock by anchoring at received
    recv_mono = timing.get("received_mono")

    def at(mono):
        if mono is None or recv_mono is None:
            return None
        return received_dt + timedelta(seconds=(mono - recv_mono))

    print(f"[Timer] Alert generated:     {hhmmss(alert_dt)}", flush=True)
    print(f"[Timer] Diagnosis received:  {hhmmss(received_dt)}", flush=True)
    if at(fired):
        print(f"[Timer] Executor fired:      {hhmmss(at(fired))}", flush=True)
    if at(deleted):
        print(f"[Timer] Pod deleted:         {hhmmss(at(deleted))}", flush=True)
    if at(running):
        print(f"[Timer] Pod running again:   {hhmmss(at(running))}", flush=True)
        total = int(round(running - recv_mono))
        print(f"[Timer] Total time to heal:  {total}s", flush=True)
    metric = diagnosis.get("metric")
    if metric == "memory_leak" and diagnosis.get("memory_mi") is not None:
        print(f"[Timer] Before crash:        pod was at {diagnosis['memory_mi']:.0f}Mi, limit is 256Mi", flush=True)
    else:
        cpu = diagnosis.get("cpu_percent", "?")
        print(f"[Timer] Before crash:        pod was at {cpu}% CPU, limit is 500m", flush=True)


# ---- per-diagnosis pipeline ------------------------------------------------
def run_swarm(diagnosis, received_dt, received_mono):
    metric = diagnosis.get("metric", "?")
    urgency = diagnosis.get("urgency", "immediate")
    pod = diagnosis.get("root_cause", "?")
    namespace = diagnosis.get("namespace", "default")

    print("=" * 40, flush=True)
    print("[Swarm] New diagnosis received!", flush=True)
    print(f"[Swarm] Metric: {metric} | Pod: {pod} | Urgency: {urgency}", flush=True)
    print("[Swarm] Starting agent pipeline...", flush=True)
    print("=" * 40, flush=True)

    # cooldown: don't restart the same pod within COOLDOWN_SECONDS
    last = last_action.get(pod, 0)
    remaining = COOLDOWN_SECONDS - (time.monotonic() - last)
    if last and remaining > 0:
        print(f"[Swarm] Pod {pod} on cooldown ({int(remaining)}s remaining). Skipping.", flush=True)
        return

    # --- routing: fast path (no LLM) vs full LLM pipeline ---
    rule = FAST_PATH_RULES.get(metric)
    if rule and urgency in rule["urgency"]:
        print(f"[Swarm] ⚡ FAST PATH activated for {metric}", flush=True)
        print(f"[Swarm] Bypassing LLM — rule-based decision: {rule['action']}", flush=True)
        decision = {"action": rule["action"], "target": pod,
                    "namespace": namespace, "urgency": urgency,
                    "reason": f"rule-based {rule['action']} for {metric}"}
        fast = True
    else:
        decision = planner(diagnosis)
        if not decision:
            return
        if not evaluator(decision):
            return
        fast = False

    last_action[pod] = time.monotonic()
    ok, timing = executor(decision)
    timing["received_mono"] = received_mono
    if ok:
        if fast:
            print("[Swarm] ✓ PROACTIVE healing complete! Pod restarted BEFORE crash.", flush=True)
        else:
            print("[Swarm] ✓ Self-healing complete!", flush=True)
        if timing.get("running") is not None:
            print(f"[Swarm] Time to fix: {int(round(timing['running'] - received_mono))}s", flush=True)
        print_timing(diagnosis, received_dt, timing)
        print("[Swarm] Waiting for next alert...", flush=True)
        print("=" * 40, flush=True)


def handle_line(line):
    received_dt = datetime.now()
    received_mono = time.monotonic()
    line = line.strip()
    if not line:
        return
    try:
        diagnosis = json.loads(line)
    except Exception:
        return  # not a JSON diagnosis line
    try:
        # act on actionable diagnoses: fast-path metrics (any urgency in the rule)
        # or critical severity for the LLM path.
        metric = diagnosis.get("metric")
        severity = diagnosis.get("severity")
        rule = FAST_PATH_RULES.get(metric)
        actionable = (rule is not None) or (severity == "critical")
        if not actionable:
            return
        if not is_fresh(diagnosis.get("timestamp")):
            print(f"[Swarm] Ignoring stale diagnosis ({diagnosis.get('timestamp')}).", flush=True)
            return
        run_swarm(diagnosis, received_dt, received_mono)
    except Exception as e:  # the swarm must never crash
        print(f"[Swarm] error handling diagnosis: {e.__class__.__name__}: {e}", flush=True)


def tail(path):
    """Yield new lines appended to `path`, tail -f style."""
    while not os.path.exists(path):
        time.sleep(0.5)
    with open(path) as f:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if line:
                yield line
            else:
                time.sleep(0.5)


def main():
    print(f"[Swarm] AI Swarm online. Model={MODEL}. Watching {DIAGNOSES_FILE}.", flush=True)
    print("[Swarm] Waiting for next alert...", flush=True)
    for line in tail(DIAGNOSES_FILE):
        handle_line(line)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[Swarm] stopped.", flush=True)
        sys.exit(0)

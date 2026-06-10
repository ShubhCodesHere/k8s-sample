#!/usr/bin/env python3
"""
eBPF-Swarm Dashboard API Server (v2).

Provides REST endpoints and Server-Sent Events (SSE) for the premium dashboard.
Key features:
- SSE `/api/events` endpoint for real-time event streaming
- `/api/config` endpoint for AI provider status
- Structured event parsing from swarm_events.json
- Thread-safe event broadcasting
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs

# Load local .env file if present
if os.path.exists(".env"):
    try:
        with open(".env") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()
    except Exception as e:
        print(f"[Warning] Failed to load .env: {e}", flush=True)

PORT = 5000
DIAGNOSES_FILE = "diagnoses.log"
SWARM_LOG = "swarm_output.log"
EVENTS_FILE = "swarm_events.json"

# Thread-safe event broadcasting for SSE
sse_clients = []
sse_lock = threading.Lock()


class LoggerWriter:
    """Redirects stdout to both the terminal and the swarm_output.log file."""

    def __init__(self, filepath):
        self.filepath = filepath
        self.terminal = sys.stdout

    def write(self, message):
        self.terminal.write(message)
        self.terminal.flush()
        try:
            with open(self.filepath, "a", encoding="utf-8") as f:
                f.write(message)
        except OSError:
            pass

    def flush(self):
        self.terminal.flush()


def broadcast_sse(event_data):
    """Send event to all connected SSE clients."""
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.put_nowait(event_data)
            except Exception:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)


def run_pipeline_thread(chaos_type):
    """Executes the SRE Agent Swarm loop in a background thread."""
    logger = LoggerWriter(SWARM_LOG)
    sys.stdout = logger

    time.sleep(0.3)
    print(f"[Swarm] Chaos event '{chaos_type}' received by backend control plane.", flush=True)

    # Generate diagnosis
    diagnosis = {
        "root_cause": "victim-app-simulation-76d9bf84c5-hj9qw",
        "metric": (
            "cpu_spike" if chaos_type == "cpu"
            else "memory_leak" if chaos_type == "memory"
            else "network_partition"
        ),
        "urgency": "immediate",
        "severity": "critical",
        "timestamp": datetime.now().isoformat(),
    }

    # Write to diagnoses.log
    try:
        with open(DIAGNOSES_FILE, "a") as f:
            f.write(json.dumps(diagnosis) + "\n")
    except OSError as e:
        print(f"[Brain] Failed to write diagnoses: {e}", flush=True)

    # Run live swarm pipeline with event monitoring
    try:
        import swarm
        # Start event monitor in parallel
        monitor = threading.Thread(target=_monitor_events, daemon=True)
        monitor.start()
        swarm.run_swarm_pipeline(diagnosis, datetime.now(), time.monotonic())
    except Exception as e:
        print(f"[Swarm] Live Agent loop failed: {str(e)}", flush=True)
        import traceback
        traceback.print_exc()
    finally:
        sys.stdout = sys.__stdout__


def _monitor_events():
    """Monitor swarm_events.json for changes and broadcast via SSE."""
    last_count = 0
    while True:
        try:
            if os.path.exists(EVENTS_FILE):
                with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                    events = json.load(f)
                if len(events) > last_count:
                    for evt in events[last_count:]:
                        broadcast_sse(json.dumps(evt))
                    last_count = len(events)
        except (json.JSONDecodeError, OSError):
            pass
        time.sleep(0.3)


class DashboardAPIHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        """Suppress noisy request logs during SSE streaming."""
        if "/api/events" not in str(args):
            super().log_message(format, *args)

    def do_GET(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/api/status":
            self.handle_status()
        elif parsed_path.path == "/api/events":
            self.handle_sse()
        elif parsed_path.path == "/api/config":
            self.handle_config()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/api/chaos":
            self.handle_chaos()
        else:
            self.send_response(404)
            self.end_headers()

    def handle_config(self):
        """Return current AI configuration status."""
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            super().end_headers()

            has_openai = bool(os.environ.get("OPENAI_API_KEY"))
            has_nvidia = bool(os.environ.get("NVIDIA_API_KEY"))
            model = os.environ.get("NVIDIA_MODEL", os.environ.get("OPENAI_MODEL", "none"))

            config = {
                "has_api_key": has_openai or has_nvidia,
                "provider": "openai" if has_openai else ("nvidia" if has_nvidia else "none"),
                "model": model,
                "mode": "live_ai" if (has_openai or has_nvidia) else "offline_demo",
            }
            self.wfile.write(json.dumps(config).encode("utf-8"))
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass

    def handle_sse(self):
        """Server-Sent Events endpoint for real-time event streaming."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

        client_queue = queue.Queue(maxsize=100)
        with sse_lock:
            sse_clients.append(client_queue)

        try:
            # Send initial connection event
            self.wfile.write(b"data: {\"type\": \"connected\"}\n\n")
            self.wfile.flush()

            while True:
                try:
                    data = client_queue.get(timeout=15)
                    self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # Send keepalive
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with sse_lock:
                if client_queue in sse_clients:
                    sse_clients.remove(client_queue)

    def handle_chaos(self):
        """Trigger a chaos event and start the SRE agent pipeline."""
        query = parse_qs(urlparse(self.path).query)
        chaos_type = query.get("type", ["cpu"])[0]

        # Reset log files for the fresh run
        try:
            with open(DIAGNOSES_FILE, "w", encoding="utf-8") as f:
                f.write("")
            with open(SWARM_LOG, "w", encoding="utf-8") as f:
                f.write("")
            with open(EVENTS_FILE, "w", encoding="utf-8") as f:
                json.dump([], f)
        except OSError:
            pass

        # Start agent pipeline execution thread
        t = threading.Thread(target=run_pipeline_thread, args=(chaos_type,))
        t.start()

        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "triggered", "type": chaos_type}).encode("utf-8"))
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass

    def get_pod_metrics(self):
        """Try to get live pod telemetry using kubectl. Fall back gracefully."""
        try:
            pod_res = subprocess.run(
                ["kubectl", "get", "pods", "-l", "app=victim-app", "-o",
                 "jsonpath={.items[0].metadata.name}"],
                capture_output=True, text=True, timeout=2,
            )
            pod_name = pod_res.stdout.strip()
            if not pod_name:
                return "offline", {"cpu": 0.0, "memory": 0, "restarts": 0, "pod_name": "unknown"}

            rest_res = subprocess.run(
                ["kubectl", "get", "pod", pod_name, "-o",
                 "jsonpath={.status.containerStatuses[0].restartCount}"],
                capture_output=True, text=True, timeout=2,
            )
            restarts = int(rest_res.stdout.strip() or 0)

            top_res = subprocess.run(
                ["kubectl", "top", "pod", pod_name, "--no-headers"],
                capture_output=True, text=True, timeout=2,
            )
            top_out = top_res.stdout.strip().split()

            cpu_val = 0.0
            mem_val = 0
            if len(top_out) >= 3:
                cpu_m = int(re.sub(r"\D", "", top_out[1]) or 0)
                mem_mi = int(re.sub(r"\D", "", top_out[2]) or 0)
                cpu_val = (cpu_m / 500.0) * 100.0
                mem_val = mem_mi

            return "live", {
                "cpu": cpu_val,
                "memory": mem_val,
                "restarts": restarts,
                "pod_name": pod_name,
                "tcpRetransmits": 0.1,
            }
        except Exception:
            return "offline", {
                "cpu": 0.0,
                "memory": 0,
                "restarts": 0,
                "pod_name": "victim-app-offline",
                "tcpRetransmits": 0.0,
            }

    def parse_events(self):
        """Parse structured events from swarm_events.json."""
        if os.path.exists(EVENTS_FILE):
            try:
                with open(EVENTS_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def parse_diagnoses(self):
        diagnoses = []
        if os.path.exists(DIAGNOSES_FILE):
            try:
                with open(DIAGNOSES_FILE, "r", encoding="utf-8") as f:
                    for line in f.readlines()[-30:]:
                        line = line.strip()
                        if line:
                            try:
                                diagnoses.append(json.loads(line))
                            except json.JSONDecodeError:
                                pass
            except OSError:
                pass
        return diagnoses

    def handle_status(self):
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()

            status, metrics = self.get_pod_metrics()
            diagnoses = self.parse_diagnoses()
            events = self.parse_events()

            response = {
                "status": status,
                "metrics": metrics,
                "diagnoses": diagnoses,
                "events": events,
            }
            self.wfile.write(json.dumps(response).encode("utf-8"))
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def run():
    server_address = ("", PORT)
    httpd = ThreadingHTTPServer(server_address, DashboardAPIHandler)
    httpd.request_queue_size = 50
    print(f"[Dashboard API] Server running on http://localhost:{PORT}")
    print(f"[Dashboard API] SSE endpoint: http://localhost:{PORT}/api/events")
    print(f"[Dashboard API] Config endpoint: http://localhost:{PORT}/api/config")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[Dashboard API] Stopping server.")
        httpd.server_close()


if __name__ == "__main__":
    run()

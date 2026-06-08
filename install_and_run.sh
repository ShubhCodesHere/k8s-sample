#!/usr/bin/env bash
#
# install_and_run.sh — set up the Snitch inside the Minikube node and run it.
# Runs on your Mac; drives the Linux node via `minikube ssh` / `minikube cp`.
#
#   chmod +x install_and_run.sh
#   ./install_and_run.sh
#
# bcc + kernel headers are installed BEST-EFFORT. On Minikube's Docker driver the
# kernel is a linuxkit build with no matching linux-headers package, so real eBPF
# won't compile -- the monitor then uses its /proc fallback, which needs only the
# node's stock python3. The install never hard-fails on that.

set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REMOTE=/home/docker/ebpf_monitor.py

blue(){ printf "\033[34m%s\033[0m\n" "$1"; }
yellow(){ printf "\033[33m%s\033[0m\n" "$1"; }

blue "==> Step 1: copy ebpf_monitor.py into the Minikube node"
minikube cp "${SCRIPT_DIR}/ebpf_monitor.py" "${REMOTE}" || {
  echo "minikube cp failed (is minikube running?)"; exit 1; }

blue "==> Step 2: best-effort install of bcc tooling inside the node"
# Split the install so a missing linux-headers package can't block bpfcc-tools.
minikube ssh -- "sudo apt-get update -qq" || yellow "apt-get update failed; continuing"
minikube ssh -- "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y bpfcc-tools python3-bpfcc" \
  || yellow "bpfcc-tools/python3-bpfcc install failed; /proc fallback will be used"
minikube ssh -- "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y linux-headers-\$(uname -r)" \
  || yellow "linux-headers for \$(uname -r) not available (expected on linuxkit) -> /proc fallback"

blue "==> Step 3: launch the monitor inside the node (sudo, foreground)"
yellow "Press Ctrl+C to stop. Trigger /stress or /crash on the victim app to see alerts."
echo
exec minikube ssh -- "sudo python3 ${REMOTE}"

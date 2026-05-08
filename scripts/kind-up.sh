#!/usr/bin/env bash
# Bring up a local Kind cluster, then delegate to install-agent-sandbox.sh
# to install the agent-sandbox controller + Python SandboxTemplate so
# harness-weaver --use-k8s works end-to-end.
#
# This is the "fresh cluster from scratch" path. If you already have a
# cluster (Docker Desktop, GKE, etc.), use install-agent-sandbox.sh
# directly instead.
#
# Idempotent: re-running is safe — existing cluster, controller, and
# template are skipped rather than recreated. Use ./scripts/kind-down.sh
# to tear it down.
#
# Environment overrides (forwarded to install-agent-sandbox.sh):
#   CLUSTER_NAME         Kind cluster name (default 'harness-weaver')
#   NAMESPACE            target namespace for the SandboxTemplate (default 'default')
#   CONTROLLER_VERSION   release tag of kubernetes-sigs/agent-sandbox (default v0.4.5)
#
# Usage:
#   ./scripts/kind-up.sh                          # defaults
#   CLUSTER_NAME=my-cluster ./scripts/kind-up.sh

set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-harness-weaver}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_SCRIPT="${SCRIPT_DIR}/install-agent-sandbox.sh"

require() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERROR: '$1' not found on PATH. Install it before running this script."
        echo "  kind:    https://kind.sigs.k8s.io/docs/user/quick-start/#installation"
        echo "  kubectl: https://kubernetes.io/docs/tasks/tools/"
        exit 1
    fi
}

echo "==> Checking prerequisites..."
require kind
require kubectl
require docker

if ! docker info >/dev/null 2>&1; then
    echo "ERROR: Docker daemon is not reachable. Start Docker before running this script."
    exit 1
fi

if [ ! -x "${INSTALL_SCRIPT}" ]; then
    echo "ERROR: ${INSTALL_SCRIPT} not found or not executable."
    echo "       This script delegates the controller install to it."
    exit 1
fi

echo "==> Bringing up Kind cluster '${CLUSTER_NAME}'..."
if kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
    echo "    Cluster '${CLUSTER_NAME}' already exists; skipping create."
else
    kind create cluster --name "${CLUSTER_NAME}" --wait 60s
fi

echo "==> Selecting kubectl context..."
kubectl config use-context "kind-${CLUSTER_NAME}"

echo "==> Delegating controller + template install to install-agent-sandbox.sh..."
# SKIP_CONFIRM=1: we just created/selected this context, no need to ask again.
# NAMESPACE and CONTROLLER_VERSION are inherited from the calling environment
# if set, otherwise install-agent-sandbox.sh applies its own defaults.
SKIP_CONFIRM=1 "${INSTALL_SCRIPT}"

echo
echo "Cluster ready. Tear down with: ./scripts/kind-down.sh"

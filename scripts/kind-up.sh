#!/usr/bin/env bash
# Bring up a local Kind cluster, install the agent-sandbox controller, and
# apply a Python SandboxTemplate so harness-weaver --use-k8s works end-to-end.
#
# Idempotent: re-running is safe — existing cluster, controller, and template
# are skipped rather than recreated. Use ./scripts/kind-down.sh to tear it
# down.
#
# Usage:
#   ./scripts/kind-up.sh                # default: cluster name harness-weaver
#   CLUSTER_NAME=my-cluster ./scripts/kind-up.sh

set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-harness-weaver}"
NAMESPACE="${NAMESPACE:-default}"
CONTROLLER_VERSION="${CONTROLLER_VERSION:-main}"
CONTROLLER_MANIFEST_URL="https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/${CONTROLLER_VERSION}/manifests/install.yaml"
TEMPLATE_PATH="$(cd "$(dirname "$0")/.." && pwd)/scripts/python-sandbox-template.yaml"

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

echo "==> Bringing up Kind cluster '${CLUSTER_NAME}'..."
if kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
    echo "    Cluster '${CLUSTER_NAME}' already exists; skipping create."
else
    kind create cluster --name "${CLUSTER_NAME}" --wait 60s
fi

echo "==> Selecting kubectl context..."
kubectl config use-context "kind-${CLUSTER_NAME}"

echo "==> Installing agent-sandbox controller (${CONTROLLER_VERSION})..."
# The upstream install manifest creates the SandboxTemplate / Sandbox CRDs
# and the controller deployment in the agent-sandbox-system namespace.
if ! kubectl apply -f "${CONTROLLER_MANIFEST_URL}" 2>/dev/null; then
    echo "ERROR: failed to apply controller manifest from ${CONTROLLER_MANIFEST_URL}"
    echo "       Check your network access and that the URL is reachable."
    exit 1
fi

echo "==> Waiting for controller to become ready..."
kubectl -n agent-sandbox-system wait \
    --for=condition=Available deployment \
    --all --timeout=180s || {
    echo "ERROR: agent-sandbox controller did not become ready within 3 minutes."
    echo "       Check 'kubectl -n agent-sandbox-system get pods' for details."
    exit 1
}

echo "==> Applying the 'python' SandboxTemplate to namespace '${NAMESPACE}'..."
kubectl apply -n "${NAMESPACE}" -f "${TEMPLATE_PATH}"

echo
echo "Cluster ready. Next steps:"
echo "  source .env                        # ensure ANTHROPIC_API_KEY is set"
echo "  harness-weaver run \\"
echo "      examples/tasks/analytical-runtime-rating.json \\"
echo "      -c single-agent-with-sandbox \\"
echo "      --model claude-haiku-4-5-20251001 \\"
echo "      --use-k8s"
echo
echo "Tear down with: ./scripts/kind-down.sh"

#!/usr/bin/env bash
# Install the agent-sandbox controller + Python SandboxTemplate into the
# *current* kubectl context — no cluster management. Use this when you
# already have a Kubernetes cluster running (Kind via Docker Desktop,
# minikube, GKE, etc.) and just want to add the sandbox infrastructure.
#
# This is idempotent and safe to re-run.
#
# What it does, in order:
#   1. Verifies kubectl is on PATH.
#   2. Shows the kubectl context it's about to modify (one last chance to
#      Ctrl-C if you're aimed at the wrong cluster).
#   3. Applies the upstream core manifest (Sandbox CRD + controller) and
#      the matching extensions manifest (SandboxClaim, SandboxTemplate CRDs,
#      RBAC). The k8s-agent-sandbox Python client uses SandboxClaim and
#      requires both artifacts from the same release tag.
#   4. Waits up to 3 minutes for the controller to become Ready after each
#      apply (extensions may roll the Deployment).
#   5. Applies the bundled scripts/python-sandbox-template.yaml to your
#      target namespace (default: 'default') so the harness's
#      AgentSandboxBackend can spawn pods from template "python".
#
# Environment overrides:
#   NAMESPACE            target namespace for the SandboxTemplate (default 'default')
#   CONTROLLER_VERSION   release tag of kubernetes-sigs/agent-sandbox (default
#                        v0.4.5 — kept in lockstep with the k8s-agent-sandbox
#                        Python SDK pinned in pyproject.toml at >=0.4,<0.5).
#                        See https://github.com/kubernetes-sigs/agent-sandbox/releases
#                        for available tags.
#   SKIP_CONFIRM         set to 1 to skip the context confirmation
#
# Usage:
#   ./scripts/install-agent-sandbox.sh                  # defaults
#   NAMESPACE=harness ./scripts/install-agent-sandbox.sh
#   SKIP_CONFIRM=1 ./scripts/install-agent-sandbox.sh   # for CI

set -euo pipefail

NAMESPACE="${NAMESPACE:-default}"
CONTROLLER_VERSION="${CONTROLLER_VERSION:-v0.4.5}"
SKIP_CONFIRM="${SKIP_CONFIRM:-0}"
# Official release artifact — produced by the upstream release pipeline,
# not a raw file pulled from a branch. See README.md of
# kubernetes-sigs/agent-sandbox for the documented install command.
CONTROLLER_MANIFEST_URL="https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${CONTROLLER_VERSION}/manifest.yaml"
EXTENSIONS_MANIFEST_URL="https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${CONTROLLER_VERSION}/extensions.yaml"
TEMPLATE_PATH="$(cd "$(dirname "$0")/.." && pwd)/scripts/python-sandbox-template.yaml"

require() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "ERROR: '$1' not found on PATH."
        echo "  Install kubectl from: https://kubernetes.io/docs/tasks/tools/"
        exit 1
    fi
}

echo "==> Checking prerequisites..."
require kubectl

CURRENT_CONTEXT=$(kubectl config current-context 2>/dev/null || true)
if [ -z "${CURRENT_CONTEXT}" ]; then
    echo "ERROR: kubectl has no current context."
    echo "       Set one with: kubectl config use-context <name>"
    exit 1
fi

# Sanity check that the cluster is actually reachable.
if ! kubectl cluster-info >/dev/null 2>&1; then
    echo "ERROR: kubectl can't reach the cluster for context '${CURRENT_CONTEXT}'."
    echo "       Is your cluster running? (Docker Desktop / Kind / minikube)"
    exit 1
fi

echo "    Context: ${CURRENT_CONTEXT}"
echo "    Namespace: ${NAMESPACE}"
echo "    Controller version: ${CONTROLLER_VERSION}"

if [ "${SKIP_CONFIRM}" != "1" ]; then
    echo
    echo "About to install the agent-sandbox controller into '${CURRENT_CONTEXT}'."
    read -r -p "Continue? [y/N] " response
    case "${response}" in
        [yY]|[yY][eE][sS]) ;;
        *) echo "Aborted."; exit 0 ;;
    esac
fi

echo
echo "==> Applying agent-sandbox controller manifest..."
# Don't silence stderr — when this fails (network, RBAC, manifest 404)
# the kubectl error is the most useful diagnostic.
if ! kubectl apply -f "${CONTROLLER_MANIFEST_URL}"; then
    echo "ERROR: failed to apply controller manifest from ${CONTROLLER_MANIFEST_URL}"
    echo "       See the kubectl error above for the specific cause."
    exit 1
fi

echo
echo "==> Waiting for controller to become ready (up to 3 minutes)..."
if ! kubectl -n agent-sandbox-system wait \
    --for=condition=Available deployment \
    --all --timeout=180s; then
    echo "ERROR: controller did not become ready within 3 minutes."
    echo "       Inspect: kubectl -n agent-sandbox-system get pods"
    echo "                kubectl -n agent-sandbox-system describe deployment"
    exit 1
fi

echo
echo "==> Applying agent-sandbox extensions manifest (SandboxClaim / SandboxTemplate CRDs)..."
if ! kubectl apply -f "${EXTENSIONS_MANIFEST_URL}"; then
    echo "ERROR: failed to apply extensions manifest from ${EXTENSIONS_MANIFEST_URL}"
    echo "       See the kubectl error above for the specific cause."
    exit 1
fi

echo
echo "==> Waiting for controller rollout after extensions (up to 3 minutes)..."
if ! kubectl -n agent-sandbox-system wait \
    --for=condition=Available deployment \
    --all --timeout=180s; then
    echo "ERROR: controller did not become ready within 3 minutes after extensions."
    echo "       Inspect: kubectl -n agent-sandbox-system get pods"
    exit 1
fi

echo
echo "==> Applying 'python' SandboxTemplate to namespace '${NAMESPACE}'..."
# Make sure the namespace exists; default 'default' always does, but a
# user-supplied namespace might not.
kubectl get namespace "${NAMESPACE}" >/dev/null 2>&1 || \
    kubectl create namespace "${NAMESPACE}"

kubectl apply -n "${NAMESPACE}" -f "${TEMPLATE_PATH}"

echo
echo "==> Verifying installation..."
kubectl -n agent-sandbox-system get pods
echo
kubectl get sandboxtemplate -n "${NAMESPACE}"

echo
echo "Agent-sandbox is installed and ready."
echo
echo "Next steps:"
echo "  1. Make sure ANTHROPIC_API_KEY is set:"
echo "       export ANTHROPIC_API_KEY=sk-ant-..."
echo "  2. Make sure harness-weaver and its deps are installed:"
echo "       pip install -e \".[dev]\""
echo "  3. Run a task with the K8s backend:"
echo "       harness-weaver run \\"
echo "         examples/tasks/analytical-runtime-rating.json \\"
echo "         -c single-agent-with-sandbox \\"
echo "         --model claude-haiku-4-5-20251001 \\"
echo "         --use-k8s"
echo
echo "To uninstall later:"
echo "  kubectl delete -n ${NAMESPACE} -f scripts/python-sandbox-template.yaml"
echo "  kubectl delete -f ${EXTENSIONS_MANIFEST_URL}"
echo "  kubectl delete -f ${CONTROLLER_MANIFEST_URL}"

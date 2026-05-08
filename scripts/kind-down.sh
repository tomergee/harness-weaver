#!/usr/bin/env bash
# Tear down the Kind cluster created by ./scripts/kind-up.sh.
#
# Idempotent: missing cluster is fine, exits 0.

set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-harness-weaver}"

if ! command -v kind >/dev/null 2>&1; then
    echo "kind is not on PATH; nothing to tear down."
    exit 0
fi

if ! kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
    echo "Cluster '${CLUSTER_NAME}' is not running; nothing to tear down."
    exit 0
fi

echo "==> Deleting Kind cluster '${CLUSTER_NAME}'..."
kind delete cluster --name "${CLUSTER_NAME}"
echo "Done."

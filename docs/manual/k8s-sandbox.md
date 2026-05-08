# Running with the Kubernetes sandbox

`run_python` runs through whatever `ExecutionBackend` is wired into the
Harness. Two backends ship:

| Backend | Isolation | Setup cost | When to use |
|---|---|---|---|
| `LocalSubprocessBackend` (default) | env scrub + fresh tmp dir | none | dev, CI, anywhere you trust the snippet enough to run as your user |
| `AgentSandboxBackend` | full pod, network policy, resource limits | one cluster, one CRD | demos, untrusted snippets, anywhere "real isolation" matters |

This page covers two paths to a working K8s backend: using a cluster
you already have (Docker Desktop's Kubernetes, GKE, EKS, etc.), and
spinning a throwaway Kind cluster from scratch.

## Prerequisites

* **kubectl** — [install instructions](https://kubernetes.io/docs/tasks/tools/)
* **A running Kubernetes cluster.** Anything `kubectl cluster-info`
  can reach works:
  * Docker Desktop's built-in Kubernetes
  * Kind (with Docker)
  * minikube
  * any managed cluster (GKE, EKS, AKS)
* The harness installed locally:

  ```bash
  pip install -e ".[dev]"
  ```

  The Python client this backend uses — [`k8s-agent-sandbox`][pypi-pkg]
  on PyPI — is pinned in `pyproject.toml` at `>=0.4,<0.5`, so the
  install command above pulls it in automatically. No separate
  `pip install k8s-agent-sandbox` step is needed.

[pypi-pkg]: https://pypi.org/project/k8s-agent-sandbox/

## Path A — you already have a cluster

The common case if you're using Docker Desktop's Kubernetes (or any
other already-running cluster). One command installs the controller
plus the `python` `SandboxTemplate` into your current `kubectl`
context:

```bash
make install-sandbox
```

That runs [`scripts/install-agent-sandbox.sh`](../../scripts/install-agent-sandbox.sh):

1. Verifies `kubectl` is on `PATH` and can reach the cluster.
2. Shows you the context name and asks for confirmation (set
   `SKIP_CONFIRM=1` to skip in CI).
3. Applies the **official release artifact** from
   [`kubernetes-sigs/agent-sandbox`][k8s-as] —
   `https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${CONTROLLER_VERSION}/manifest.yaml`
   — installs the `SandboxTemplate` and `Sandbox` CRDs plus the
   controller `Deployment` in `agent-sandbox-system`. Defaults to
   the **`v0.4.5`** release, which is the same minor version the
   `k8s-agent-sandbox` Python SDK is pinned to in
   `pyproject.toml`. Override with `CONTROLLER_VERSION=v0.x.y` to
   pick a different tag from
   [the releases page](https://github.com/kubernetes-sigs/agent-sandbox/releases).
4. Waits up to 3 minutes for the controller to become Ready.
5. Applies the bundled
   [`scripts/python-sandbox-template.yaml`](../../scripts/python-sandbox-template.yaml)
   to your target namespace (default: `default`) — a slim
   `python:3.11-slim` template the harness instantiates when you ask
   for `template="python"`.

Idempotent: re-running is safe.

Common overrides (set as env vars before `make install-sandbox`):

| Variable | Default | Purpose |
|---|---|---|
| `NAMESPACE` | `default` | Target namespace for the `SandboxTemplate`. Created if missing. |
| `CONTROLLER_VERSION` | `v0.4.5` | Release tag of `kubernetes-sigs/agent-sandbox` to install. The script downloads `releases/download/${CONTROLLER_VERSION}/manifest.yaml`. Keep it within the same minor as `k8s-agent-sandbox` in `pyproject.toml` (currently `>=0.4,<0.5`). |
| `SKIP_CONFIRM` | (unset) | Set to `1` to skip the "is this the right context?" prompt. |

[k8s-as]: https://github.com/kubernetes-sigs/agent-sandbox

## Path B — bring up a fresh Kind cluster

If you don't have a cluster, want a throwaway one, or want everything
torn down cleanly afterwards:

```bash
make kind-up
```

Runs [`scripts/kind-up.sh`](../../scripts/kind-up.sh): same five
steps as Path A, plus a `kind create cluster --name harness-weaver`
at the top. Tear down with `make kind-down`.

Requires Docker (with the daemon running) and `kind` ≥ 0.20 on
`PATH` ([install instructions](https://kind.sigs.k8s.io/docs/user/quick-start/#installation)).

Idempotent: re-running on top of an existing cluster is a no-op.

## Run with the K8s backend

```bash
source .env  # ensure ANTHROPIC_API_KEY is set
harness-weaver run examples/tasks/analytical-runtime-rating.json \
    --config single-agent-with-sandbox \
    --model claude-haiku-4-5-20251001 \
    --use-k8s
```

`--use-k8s` swaps `LocalSubprocessBackend` for `AgentSandboxBackend`
in the harness. The backend lazily provisions one sandbox pod the
first time the agent calls `run_python` and reuses it for the rest of
the run (see [ADR-0003](../adr/0003-sandbox-lifecycle.md) for why).
On a Kind cluster, expect the first call to take 30-60 seconds while
the pod schedules and the image pulls; subsequent calls in the same
run are a few hundred milliseconds.

The `--use-k8s` flag also works on `compare` and `eval`:

```bash
harness-weaver compare examples/tasks/analytical-runtime-rating.json \
    --config-a single-agent-basic \
    --config-b single-agent-with-sandbox \
    --model claude-haiku-4-5-20251001 \
    --use-k8s
```

## Tear down

```bash
make kind-down
```

Deletes the Kind cluster (and everything inside it). Idempotent.

## Troubleshooting

### `kubectl can't reach the cluster for context '...'`

`make install-sandbox` checks `kubectl cluster-info` first. If it
fails, your cluster isn't running or `kubectl` is pointed at the
wrong context. Diagnose:

```bash
kubectl config current-context     # which context is selected?
kubectl config get-contexts        # what's available?
kubectl cluster-info               # what does the cluster say?
```

For Docker Desktop: open Docker Desktop → Settings → Kubernetes,
make sure "Enable Kubernetes" is checked, wait for the green dot.

### `failed to connect to the docker API` (when running `make kind-up`)

Docker daemon isn't running. Start Docker Desktop (or `systemctl
start docker` on Linux) and re-run `make kind-up`. This error doesn't
apply to `make install-sandbox` — it never touches Docker directly.

### Controller doesn't become ready in 3 minutes

Check `kubectl -n agent-sandbox-system get pods` for the controller
pod's status. Common causes:

* Image pull failure (cluster can't reach the registry — check Kind's
  network on locked-down corp networks).
* Insufficient resources (Kind's default node has 2 CPU / 8 GB; the
  controller needs ~100m / 256Mi).

### `SandboxTemplate "python" not found`

The script's `kubectl apply` step didn't run, or you applied the
template to a different namespace than the harness is reading from.
Check:

```bash
kubectl get sandboxtemplate -n default
```

If empty, apply manually:

```bash
kubectl apply -n default -f scripts/python-sandbox-template.yaml
```

### `sandbox is not ready` after 180 seconds

The pod is stuck. Inspect:

```bash
kubectl get sandboxes -n default
kubectl describe sandbox <name> -n default
kubectl logs <pod-name> -n default
```

Most often: image pull stuck (corp network) or the requested
resources don't fit the node. Tweak
`scripts/python-sandbox-template.yaml` if needed and re-apply.

### Pod orphaned after a crash

By default, `AgentSandboxBackend.close()` terminates the pod. If the
harness crashed mid-run, the pod might still be there:

```bash
kubectl get sandboxes -n default
kubectl delete sandbox <name> -n default
```

Or just `make kind-down && make kind-up` — that's the nuclear option,
fast on Kind.

## Programmatic use

If you want to drive the K8s backend from Python without the CLI:

```python
from harness_weaver.execution import AgentSandboxBackend
from harness_weaver.harness import Harness
from harness_weaver.catalog import Catalog
from harness_weaver.agent_runner import RealAgentRunner

# Default config: connects to whatever cluster kubectl points at,
# uses 'default' namespace, expects the 'python' SandboxTemplate.
backend = AgentSandboxBackend()
try:
    harness = Harness(
        catalog=Catalog.load_default(),
        runner=RealAgentRunner(),
        execution_backend=backend,
    )
    # ... harness.run(...) ...
finally:
    backend.close()  # terminate the pod
```

Or use the context-manager form:

```python
with AgentSandboxBackend() as backend:
    harness = Harness(catalog=..., runner=..., execution_backend=backend)
    ...
# backend.close() ran on exit — pod terminated.
```

## What's not yet here

* **Stdin** — `AgentSandboxBackend.run` rejects requests with
  `stdin` set; supporting it would mean staging the bytes as a file
  inside the sandbox and prepending `cat /tmp/.stdin |` to the
  command. Not wired in v1.
* **Resource overrides per call** — the template's CPU/memory
  limits apply to every call. If you want a fatter pod for one
  snippet, edit the template (and consider a separate template name
  for the heavy variant).
* **Network policies** — the bundled template doesn't set any
  egress restrictions; the snippet can reach the public internet
  from inside the sandbox. Apply a `NetworkPolicy` if that matters
  for your use case.

Back to the [manual index](README.md).

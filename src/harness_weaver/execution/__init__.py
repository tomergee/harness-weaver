"""Execution backends for the dangerous ``run_python`` tool.

The :class:`ExecutionBackend` protocol is the seam between *tool definition*
and *tool execution*. ``run_python`` doesn't know whether it's executing
locally as a subprocess (dev) or in a Kubernetes-managed sandbox pod (real
demo) — it talks to whatever backend was injected into it.

Backends:
    LocalSubprocessBackend  — runs Python via subprocess on the host.
                              Dev only; no real isolation.
    AgentSandboxBackend     — wraps kubernetes-sigs/agent-sandbox.
                              Production-grade isolation; needs a cluster.
"""

from harness_weaver.execution.base import (
    ExecutionBackend,
    ExecutionRequest,
    ExecutionResult,
    ExecutionTimeoutError,
)
from harness_weaver.execution.k8s import AgentSandboxBackend
from harness_weaver.execution.local import LocalSubprocessBackend

__all__ = [
    "AgentSandboxBackend",
    "ExecutionBackend",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionTimeoutError",
    "LocalSubprocessBackend",
]

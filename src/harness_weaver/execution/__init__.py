"""Execution backends for the dangerous ``run_python`` tool.

The :class:`ExecutionBackend` protocol is the seam between *tool definition*
and *tool execution*. ``run_python`` doesn't know whether it's executing
locally as a subprocess (dev) or in a Kubernetes-managed sandbox pod (real
demo) — it talks to whatever backend was injected into it.

Backends:
    LocalSubprocessBackend  — runs Python via subprocess on the host.
                              Dev only; no real isolation.
    AgentSandboxBackend     — wraps kubernetes-sigs/agent-sandbox.
                              (Stubbed for now; lands in a follow-up.)
"""

from harness_weaver.execution.base import (
    ExecutionBackend,
    ExecutionRequest,
    ExecutionResult,
    ExecutionTimeoutError,
)
from harness_weaver.execution.local import LocalSubprocessBackend

__all__ = [
    "ExecutionBackend",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionTimeoutError",
    "LocalSubprocessBackend",
]

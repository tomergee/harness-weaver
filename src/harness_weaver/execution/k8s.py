"""AgentSandboxBackend — runs Python in a Kubernetes-managed sandbox pod.

This is the **production** execution backend. Where
:class:`LocalSubprocessBackend` runs snippets as a child process on the
host (no real isolation), this one delegates to ``k8s-agent-sandbox``,
which provisions a sandbox pod via the upstream
`kubernetes-sigs/agent-sandbox`_ controller.

Lifecycle: one sandbox per backend instance, reused across runs (ADR-0003).
The first call to :meth:`run` creates the pod; subsequent calls reuse it.
``close()`` (and the context-manager protocol) terminates it. The pod's
writable state isn't reset between calls — agents share a single sandbox
for the lifetime of a Harness — which is the right tradeoff for our use
case (cheap reuse, deterministic side-effects within a run).

Snippet transport: we write the source to ``/tmp/snippet.py`` via the
sandbox's ``Filesystem`` API and run ``python3 /tmp/snippet.py``. Going
through the file API rather than ``python3 -c '<code>'`` avoids shell
escaping issues for snippets containing quotes, backticks, or newlines.

.. _kubernetes-sigs/agent-sandbox:
   https://github.com/kubernetes-sigs/agent-sandbox
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from k8s_agent_sandbox import SandboxClient, SandboxError

from harness_weaver.execution.base import (
    ExecutionBackend,
    ExecutionRequest,
    ExecutionResult,
)

if TYPE_CHECKING:
    from types import TracebackType

    from k8s_agent_sandbox.sandbox import Sandbox

DEFAULT_TEMPLATE = "python"
"""Sandbox template name. Resolved against the cluster's installed
``SandboxTemplate`` resources; the user is expected to have applied a
template named this in the target namespace before running. Override via
the ``template`` constructor argument."""

DEFAULT_NAMESPACE = "default"
DEFAULT_SANDBOX_READY_TIMEOUT_SECONDS = 180
SNIPPET_PATH = "/tmp/snippet.py"


class AgentSandboxBackend(ExecutionBackend):
    """K8s-backed execution: warm sandbox, reused across runs.

    Args:
        template: Name of the ``SandboxTemplate`` to instantiate. Must
            already exist in ``namespace``. Defaults to ``"python"``.
        namespace: Kubernetes namespace the sandbox lives in.
        connection_config: How the client reaches the sandbox. ``None``
            (the default) means the SDK picks a local tunnel via
            ``kubectl port-forward``-style behavior, which is the right
            choice for Kind / local development. For in-cluster use,
            pass :class:`SandboxInClusterConnectionConfig`.
        sandbox_ready_timeout: Seconds to wait for the sandbox pod to
            reach Ready. Default 180; bump if your cluster is slow to
            schedule.
        cleanup_on_close: When True, ``close()`` terminates the sandbox
            in addition to closing the connection. Default True — we
            don't want orphaned pods after a run. Set False if you want
            to keep the sandbox around for inspection.
    """

    def __init__(
        self,
        *,
        template: str = DEFAULT_TEMPLATE,
        namespace: str = DEFAULT_NAMESPACE,
        connection_config: object | None = None,
        sandbox_ready_timeout: int = DEFAULT_SANDBOX_READY_TIMEOUT_SECONDS,
        cleanup_on_close: bool = True,
        client: SandboxClient | None = None,
    ) -> None:
        # ``client`` is exposed so tests can inject a mock without
        # touching kubectl. Production callers leave it None and the
        # backend constructs its own.
        self._client: SandboxClient = client or SandboxClient(
            connection_config=connection_config,
        )
        self._template = template
        self._namespace = namespace
        self._ready_timeout = sandbox_ready_timeout
        self._cleanup_on_close = cleanup_on_close
        self._sandbox: Sandbox | None = None

    # --- ExecutionBackend protocol ------------------------------------

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        if request.stdin is not None:
            # Supporting stdin would require staging it as a file and
            # piping into the snippet — doable but adds a second file
            # write per call. Defer until a real use case shows up.
            raise NotImplementedError(
                "AgentSandboxBackend does not support stdin yet. "
                "Stage data into your snippet via files or environment "
                "variables, or use LocalSubprocessBackend for now."
            )

        sandbox = self._ensure_sandbox()
        # File-staging the snippet sidesteps shell-escape pitfalls when
        # the code contains quotes, backslashes, or newlines.
        sandbox.files.write(SNIPPET_PATH, request.code, timeout=int(request.timeout_seconds))

        start = time.monotonic()
        try:
            sb_result = sandbox.commands.run(
                f"python3 {SNIPPET_PATH}",
                timeout=int(request.timeout_seconds),
            )
            duration = time.monotonic() - start
            return ExecutionResult(
                exit_code=sb_result.exit_code,
                stdout=sb_result.stdout,
                stderr=sb_result.stderr,
                timed_out=False,
                duration_seconds=duration,
            )
        except SandboxError as exc:
            duration = time.monotonic() - start
            # Best-effort timeout detection: if we burned through the
            # configured budget, treat as a timeout result rather than
            # propagating the SDK exception. Otherwise the failure is
            # infrastructure (port-forward broken, RBAC, etc.) and the
            # caller should see it.
            if duration >= request.timeout_seconds * 0.9:
                return ExecutionResult(
                    exit_code=-9,
                    stdout="",
                    stderr=f"sandbox call timed out after {duration:.1f}s: {exc}",
                    timed_out=True,
                    duration_seconds=duration,
                )
            raise

    # --- lifecycle -----------------------------------------------------

    def _ensure_sandbox(self) -> Sandbox:
        if self._sandbox is None:
            self._sandbox = self._client.create_sandbox(
                template=self._template,
                namespace=self._namespace,
                sandbox_ready_timeout=self._ready_timeout,
            )
        return self._sandbox

    def close(self) -> None:
        """Close the sandbox connection and (optionally) terminate the pod.

        Idempotent: calling ``close()`` twice is a no-op. After ``close()``,
        the next ``run()`` provisions a fresh sandbox.
        """
        if self._sandbox is None:
            return
        try:
            self._sandbox.close_connection()
            if self._cleanup_on_close:
                self._sandbox.terminate()
        finally:
            self._sandbox = None

    def __enter__(self) -> AgentSandboxBackend:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, tb
        self.close()


__all__ = [
    "DEFAULT_NAMESPACE",
    "DEFAULT_SANDBOX_READY_TIMEOUT_SECONDS",
    "DEFAULT_TEMPLATE",
    "SNIPPET_PATH",
    "AgentSandboxBackend",
]

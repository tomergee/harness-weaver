"""ExecutionBackend protocol and value types.

A backend takes an :class:`ExecutionRequest` (code + timeout + optional
stdin) and returns an :class:`ExecutionResult` (exit code + captured streams
+ wall-clock duration). Timeouts are surfaced as ``timed_out=True`` in the
result rather than as exceptions, because the agent should be allowed to
reason about the failure rather than have execution control flow blow up
the whole turn. Genuinely unrecoverable backend errors (e.g. the K8s pod
crashed before it could run anything) still raise.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ExecutionTimeoutError(Exception):
    """Raised when a backend cannot enforce its own timeout (defensive fallback).

    Normal timeouts come back as ``ExecutionResult(timed_out=True)``; this
    exception is reserved for backend-internal failures where the timeout
    enforcement itself broke.
    """


@dataclass(frozen=True)
class ExecutionRequest:
    """Inputs to a single sandboxed code execution."""

    code: str
    timeout_seconds: float = 30.0
    stdin: str | None = None


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of a single sandboxed code execution.

    ``timed_out`` is set when the backend killed the process at the timeout
    boundary; ``exit_code`` is undefined in that case (typically -SIGKILL on
    Unix). ``duration_seconds`` is wall-clock from invocation to return,
    not CPU time.
    """

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class ExecutionBackend(ABC):
    """Abstract base for anything that can execute a Python snippet.

    Concrete backends are stateless from the caller's perspective: each
    ``run`` is independent. Backends that need warm state (e.g. a
    long-lived sandbox pod) hide that behind ``__enter__``/``__exit__``;
    ADR-0003 will codify the lifecycle.
    """

    @abstractmethod
    def run(self, request: ExecutionRequest) -> ExecutionResult:
        """Execute ``request.code`` and return what happened.

        Implementations must not raise on non-zero exit, captured stderr,
        or timeout — those are normal results. They may raise on backend
        infrastructure failures (broken pipe to a sandbox pod, missing
        Python interpreter, etc.).
        """


__all__ = [
    "ExecutionBackend",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionTimeoutError",
]

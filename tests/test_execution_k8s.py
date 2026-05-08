"""Unit tests for AgentSandboxBackend.

The k8s-agent-sandbox SDK is mocked: tests don't talk to a real cluster.
We're verifying:

* Lazy sandbox creation (one per backend instance).
* Sandbox reuse across calls (warm pattern, ADR-0003).
* File staging via ``files.write`` and execution via ``commands.run``.
* Result mapping from SDK ``ExecutionResult`` to ours.
* Timeout detection on SandboxError.
* ``close()`` semantics: idempotent, terminates by default, swappable.
* Stdin rejection (not supported in v1).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from k8s_agent_sandbox import SandboxError

from harness_weaver.execution import (
    AgentSandboxBackend,
    ExecutionRequest,
)
from harness_weaver.execution.k8s import SNIPPET_PATH


def _sdk_result(*, stdout: str = "", stderr: str = "", exit_code: int = 0) -> Any:
    """Mimic ``k8s_agent_sandbox.models.ExecutionResult``."""
    sb_result = MagicMock()
    sb_result.stdout = stdout
    sb_result.stderr = stderr
    sb_result.exit_code = exit_code
    return sb_result


def _fake_sandbox(*, run_returns: Any = None, run_raises: Exception | None = None) -> MagicMock:
    sandbox = MagicMock()
    if run_raises is not None:
        sandbox.commands.run.side_effect = run_raises
    else:
        sandbox.commands.run.return_value = run_returns or _sdk_result(stdout="ok")
    return sandbox


def _fake_client(sandbox: MagicMock) -> MagicMock:
    client = MagicMock()
    client.create_sandbox.return_value = sandbox
    return client


# --- Lazy creation + reuse ---------------------------------------------


class TestSandboxLifecycle:
    def test_sandbox_not_created_until_first_run(self) -> None:
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        AgentSandboxBackend(client=client)
        assert client.create_sandbox.call_count == 0

    def test_sandbox_created_on_first_run(self) -> None:
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client, template="python", namespace="harness")
        backend.run(ExecutionRequest(code="print(1)"))
        client.create_sandbox.assert_called_once_with(
            template="python", namespace="harness", sandbox_ready_timeout=180
        )

    def test_sandbox_reused_across_calls(self) -> None:
        # Three runs → one create_sandbox; the warm-sandbox-per-Harness
        # pattern (ADR-0003).
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        for _ in range(3):
            backend.run(ExecutionRequest(code="print(1)"))
        assert client.create_sandbox.call_count == 1
        assert sandbox.commands.run.call_count == 3

    def test_close_is_idempotent(self) -> None:
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        backend.run(ExecutionRequest(code="print(1)"))
        backend.close()
        backend.close()  # second close is a no-op
        # Only one terminate even though we called close twice.
        assert sandbox.terminate.call_count == 1
        assert sandbox.close_connection.call_count == 1

    def test_close_terminates_by_default(self) -> None:
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        backend.run(ExecutionRequest(code="print(1)"))
        backend.close()
        sandbox.terminate.assert_called_once()

    def test_cleanup_on_close_false_keeps_sandbox(self) -> None:
        # Useful for "let me poke at the sandbox after the run" debugging.
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client, cleanup_on_close=False)
        backend.run(ExecutionRequest(code="print(1)"))
        backend.close()
        sandbox.close_connection.assert_called_once()
        sandbox.terminate.assert_not_called()

    def test_close_then_run_creates_fresh_sandbox(self) -> None:
        sandbox_1 = _fake_sandbox()
        sandbox_2 = _fake_sandbox()
        client = MagicMock()
        client.create_sandbox.side_effect = [sandbox_1, sandbox_2]
        backend = AgentSandboxBackend(client=client)
        backend.run(ExecutionRequest(code="print(1)"))
        backend.close()
        backend.run(ExecutionRequest(code="print(2)"))
        assert client.create_sandbox.call_count == 2

    def test_context_manager_closes_on_exit(self) -> None:
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        with AgentSandboxBackend(client=client) as backend:
            backend.run(ExecutionRequest(code="print(1)"))
        sandbox.terminate.assert_called_once()


# --- Snippet transport + result mapping --------------------------------


class TestRunMechanics:
    def test_snippet_staged_via_files_write(self) -> None:
        # File-staging avoids shell escaping; verify we used Filesystem.
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        code = 'print("quotes \' and `backticks` and \\nnewlines")'
        backend.run(ExecutionRequest(code=code, timeout_seconds=20))

        # files.write should have received the snippet verbatim.
        sandbox.files.write.assert_called_once_with(SNIPPET_PATH, code, timeout=20)

    def test_command_runs_the_staged_file(self) -> None:
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        backend.run(ExecutionRequest(code="print(1)", timeout_seconds=15))
        sandbox.commands.run.assert_called_once_with(f"python3 {SNIPPET_PATH}", timeout=15)

    def test_files_then_commands_call_order(self) -> None:
        # The snippet must be on disk before we run it; verify ordering.
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        # Build a manager mock so we can observe the call order across
        # both nested mocks.
        order_tracker = MagicMock()
        order_tracker.attach_mock(sandbox.files.write, "write")
        order_tracker.attach_mock(sandbox.commands.run, "run")
        backend.run(ExecutionRequest(code="print(1)"))
        names = [c[0] for c in order_tracker.mock_calls]
        assert names == ["write", "run"]

    def test_result_fields_round_trip(self) -> None:
        sandbox = _fake_sandbox(
            run_returns=_sdk_result(stdout="hello\n", stderr="warn", exit_code=0)
        )
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        result = backend.run(ExecutionRequest(code="print('hello')"))
        assert result.stdout == "hello\n"
        assert result.stderr == "warn"
        assert result.exit_code == 0
        assert result.timed_out is False
        assert result.duration_seconds >= 0.0

    def test_nonzero_exit_passed_through(self) -> None:
        sandbox = _fake_sandbox(run_returns=_sdk_result(stdout="", stderr="oops", exit_code=2))
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        result = backend.run(ExecutionRequest(code="raise SystemExit(2)"))
        assert result.exit_code == 2
        assert result.succeeded is False


# --- Timeout / error handling -----------------------------------------


class TestTimeoutHandling:
    def test_quick_sandbox_error_propagates(self) -> None:
        # A SandboxError that fires fast (well before the timeout budget)
        # is infrastructure trouble — propagate so the caller sees it.
        sandbox = _fake_sandbox(run_raises=SandboxError("port-forward broken"))
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        with pytest.raises(SandboxError, match="port-forward"):
            backend.run(ExecutionRequest(code="print(1)", timeout_seconds=30))

    def test_slow_sandbox_error_treated_as_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Fake the clock so the elapsed-time heuristic fires deterministically.
        # We pretend the SandboxError happened after the configured timeout.
        clock = iter([100.0, 105.0])  # start, end (5s elapsed)

        def fake_monotonic() -> float:
            return next(clock)

        monkeypatch.setattr("harness_weaver.execution.k8s.time.monotonic", fake_monotonic)

        sandbox = _fake_sandbox(run_raises=SandboxError("read timed out"))
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        # 5s budget; the error happens at 5s elapsed → treated as timeout.
        result = backend.run(ExecutionRequest(code="while True: pass", timeout_seconds=5.0))
        assert result.timed_out is True
        assert result.exit_code == -9
        assert "timed out" in result.stderr.lower()


# --- v1 limitations ----------------------------------------------------


class TestStdinRejected:
    def test_stdin_raises_not_implemented(self) -> None:
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        with pytest.raises(NotImplementedError, match="stdin"):
            backend.run(ExecutionRequest(code="print(1)", stdin="data"))

    def test_stdin_rejection_does_not_create_sandbox(self) -> None:
        # The check fires before any cluster work — rejecting bad input
        # shouldn't waste a pod.
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        with pytest.raises(NotImplementedError):
            backend.run(ExecutionRequest(code="print(1)", stdin="data"))
        client.create_sandbox.assert_not_called()

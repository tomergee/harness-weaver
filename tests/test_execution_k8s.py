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


# --- PR #6 review fixes ---------------------------------------------


class TestFractionalTimeoutRoundsUp:
    """Regression for PR #6 review (HIGH): ``int(0.5)`` truncates to 0,
    which the SDK reads as "give up immediately." Sub-second timeouts
    must round *up* to at least 1 second so the SDK actually attempts
    the call."""

    def test_sub_second_timeout_floors_at_one(self) -> None:
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        backend.run(ExecutionRequest(code="print(1)", timeout_seconds=0.5))
        # Both the file write and the command run must see >= 1.
        sandbox.files.write.assert_called_once_with(SNIPPET_PATH, "print(1)", timeout=1)
        sandbox.commands.run.assert_called_once_with(f"python3 {SNIPPET_PATH}", timeout=1)

    def test_fractional_timeout_rounds_up_not_down(self) -> None:
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        # 5.1s → 6, not 5. Truncating would silently shorten the budget.
        backend.run(ExecutionRequest(code="print(1)", timeout_seconds=5.1))
        sandbox.commands.run.assert_called_once_with(f"python3 {SNIPPET_PATH}", timeout=6)

    def test_integer_timeout_passes_through_unchanged(self) -> None:
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        backend.run(ExecutionRequest(code="print(1)", timeout_seconds=30))
        sandbox.commands.run.assert_called_once_with(f"python3 {SNIPPET_PATH}", timeout=30)


class TestCloseResilientToCloseConnectionError:
    """Regression for PR #6 review (MED): ``close_connection`` raising
    must not skip ``terminate``. Otherwise we leave an orphan pod
    every time the connection-close path errors."""

    def test_close_connection_error_does_not_skip_terminate(self) -> None:
        sandbox = _fake_sandbox()
        sandbox.close_connection.side_effect = RuntimeError("port-forward dead")
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        backend.run(ExecutionRequest(code="print(1)"))
        with pytest.raises(RuntimeError, match="port-forward"):
            backend.close()
        # terminate was attempted despite the close_connection failure.
        sandbox.terminate.assert_called_once()

    def test_close_idempotent_after_partial_failure(self) -> None:
        sandbox = _fake_sandbox()
        sandbox.close_connection.side_effect = RuntimeError("oops")
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        backend.run(ExecutionRequest(code="print(1)"))
        with pytest.raises(RuntimeError):
            backend.close()
        # Even after a partial failure, the sandbox reference is cleared
        # so a second close() is a clean no-op (rather than re-attempting
        # operations on a broken handle).
        backend.close()  # must not raise
        # Each operation called exactly once across the two close() calls.
        assert sandbox.close_connection.call_count == 1
        assert sandbox.terminate.call_count == 1


class TestThreadingLockUsed:
    """Regression for PR #6 review (MED): concurrent run() calls would
    have raced to overwrite ``/tmp/snippet.py``. We protect run() and
    close() with a Lock; verify it's actually wired."""

    def test_run_acquires_lock(self) -> None:
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        # Replace the lock with a MagicMock so we can observe acquire/release.
        observed_lock = MagicMock()
        observed_lock.__enter__ = MagicMock(return_value=None)
        observed_lock.__exit__ = MagicMock(return_value=False)
        backend._lock = observed_lock  # type: ignore[assignment]

        backend.run(ExecutionRequest(code="print(1)"))
        observed_lock.__enter__.assert_called()
        observed_lock.__exit__.assert_called()

    def test_close_acquires_lock(self) -> None:
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        backend.run(ExecutionRequest(code="print(1)"))

        observed_lock = MagicMock()
        observed_lock.__enter__ = MagicMock(return_value=None)
        observed_lock.__exit__ = MagicMock(return_value=False)
        backend._lock = observed_lock  # type: ignore[assignment]
        backend.close()
        observed_lock.__enter__.assert_called()


class TestTelemetry:
    """Sandbox telemetry: ``call_count`` / ``total_call_seconds`` /
    ``started_at`` get accumulated across run() calls and exposed via
    ``telemetry()``. Lazy: ``telemetry()`` returns None when no pod
    was ever provisioned (the chosen configuration didn't expose
    run_python so the harness never reached the backend).
    """

    def test_telemetry_none_before_first_run(self) -> None:
        client = _fake_client(_fake_sandbox())
        backend = AgentSandboxBackend(client=client)
        assert backend.telemetry() is None

    def test_telemetry_records_call_after_run(self) -> None:
        sandbox = _fake_sandbox()
        sandbox.name = "sb-abc123"
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client, namespace="harness", template="python")
        backend.run(ExecutionRequest(code="print(1)"))

        tel = backend.telemetry()
        assert tel is not None
        assert tel.namespace == "harness"
        assert tel.template == "python"
        assert tel.pod_name == "sb-abc123"
        assert tel.call_count == 1
        assert tel.total_call_seconds >= 0.0
        # started_at is timezone-aware; we don't pin the exact value.
        assert tel.started_at.tzinfo is not None

    def test_telemetry_accumulates_across_calls(self) -> None:
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        backend.run(ExecutionRequest(code="print(1)"))
        backend.run(ExecutionRequest(code="print(2)"))
        backend.run(ExecutionRequest(code="print(3)"))
        tel = backend.telemetry()
        assert tel is not None
        assert tel.call_count == 3

    def test_telemetry_counts_failed_calls_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Even SandboxError-treated-as-timeout calls count toward
        telemetry — the pod time was paid for either way."""
        sandbox = _fake_sandbox(run_raises=SandboxError("kaboom"))
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        # Force the timeout-detection branch: pretend the call burned
        # most of the budget.
        import harness_weaver.execution.k8s as mod

        clock = iter([0.0, 100.0])
        monkeypatch.setattr(mod.time, "monotonic", lambda: next(clock))
        result = backend.run(ExecutionRequest(code="print(1)", timeout_seconds=10.0))
        assert result.timed_out
        tel = backend.telemetry()
        assert tel is not None
        assert tel.call_count == 1
        assert tel.total_call_seconds >= 90.0

    def test_average_call_seconds_zero_when_no_calls(self) -> None:
        """``average_call_seconds`` short-circuits the divide-by-zero
        when call_count is 0 (e.g. the pod was provisioned but every
        run() call raised before completing)."""
        from datetime import UTC, datetime

        from harness_weaver.trajectory import SandboxTelemetry

        tel = SandboxTelemetry(
            pod_name="sb-x",
            namespace="default",
            template="python",
            started_at=datetime.now(UTC),
            call_count=0,
            total_call_seconds=0.0,
        )
        assert tel.average_call_seconds == 0.0

    def test_telemetry_resets_counters_after_read(self) -> None:
        """PR #20 review: counters reset on each telemetry() read.

        In multi-task flows (``eval``, ``compare``) the Harness calls
        telemetry() after every trajectory; without the reset, each
        successive trajectory's telemetry would carry the previous
        ones' activity and the CLI summary's aggregate-across-list
        would double-count. ``started_at`` does *not* reset — the pod
        is still the same pod.
        """
        sandbox = _fake_sandbox()
        client = _fake_client(sandbox)
        backend = AgentSandboxBackend(client=client)
        backend.run(ExecutionRequest(code="print(1)"))
        backend.run(ExecutionRequest(code="print(2)"))

        first = backend.telemetry()
        assert first is not None
        assert first.call_count == 2

        # No further run() calls in between; second read should be
        # zero counts but the same provisioning timestamp.
        second = backend.telemetry()
        assert second is not None
        assert second.call_count == 0
        assert second.total_call_seconds == 0.0
        assert second.started_at == first.started_at

    def test_telemetry_resets_when_fresh_pod_is_provisioned(self) -> None:
        """PR #20 review: close() leaves the counters where they were,
        but the next ``run()`` provisions a fresh pod via
        ``_ensure_sandbox``, which must reset the counters so the new
        pod's stats don't include the previous pod's activity.
        ``started_at`` advances to the new pod's provisioning time.
        """
        sandbox_1 = _fake_sandbox()
        sandbox_2 = _fake_sandbox()
        client = MagicMock()
        client.create_sandbox.side_effect = [sandbox_1, sandbox_2]
        backend = AgentSandboxBackend(client=client)
        backend.run(ExecutionRequest(code="print(1)"))
        backend.run(ExecutionRequest(code="print(2)"))
        first = backend.telemetry()
        assert first is not None
        assert first.call_count == 2
        first_started_at = first.started_at

        # Close terminates the pod; the next run gets a fresh one.
        backend.close()
        backend.run(ExecutionRequest(code="print(3)"))
        second = backend.telemetry()
        assert second is not None
        # The new pod has *only* the one call we made under it,
        # not 2 + 1.
        assert second.call_count == 1
        # started_at advanced to the new pod's provisioning time.
        assert second.started_at >= first_started_at

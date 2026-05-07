"""Unit tests for the run_python tool.

These tests use a real LocalSubprocessBackend rather than a mock, because
the seam between tool and backend is the thing we want to verify.
A separate FakeBackend is used where we need to assert *behavior at the
tool layer* without spawning a subprocess (faster, deterministic).
"""

import pytest

from harness_weaver.execution import (
    ExecutionBackend,
    ExecutionRequest,
    ExecutionResult,
    LocalSubprocessBackend,
)
from harness_weaver.tools import RunPythonTool, Tool, ToolError, ToolRegistry


class _FakeBackend(ExecutionBackend):
    """Records the request it received and returns a canned result."""

    def __init__(self, result: ExecutionResult) -> None:
        self._result = result
        self.last_request: ExecutionRequest | None = None

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        self.last_request = request
        return self._result


_OK = ExecutionResult(
    exit_code=0,
    stdout="hello\n",
    stderr="",
    timed_out=False,
    duration_seconds=0.05,
)


class TestRunPythonContract:
    def test_call_returns_serializable_dict(self) -> None:
        tool = RunPythonTool(_FakeBackend(_OK))
        result = tool.call({"code": "print('hello')"})
        assert result["stdout"] == "hello\n"
        assert result["exit_code"] == 0
        assert result["succeeded"] is True

    def test_succeeded_field_derived_from_exit_and_timeout(self) -> None:
        timed_out = ExecutionResult(
            exit_code=-9, stdout="", stderr="", timed_out=True, duration_seconds=1.0
        )
        tool = RunPythonTool(_FakeBackend(timed_out))
        result = tool.call({"code": "while True: pass"})
        assert result["timed_out"] is True
        assert result["succeeded"] is False

    def test_input_validation_rejects_empty_code(self) -> None:
        tool = RunPythonTool(_FakeBackend(_OK))
        with pytest.raises(ToolError, match="invalid arguments"):
            tool.call({"code": ""})

    def test_input_validation_rejects_zero_timeout(self) -> None:
        tool = RunPythonTool(_FakeBackend(_OK))
        with pytest.raises(ToolError, match="invalid arguments"):
            tool.call({"code": "print(1)", "timeout_seconds": 0})

    def test_input_validation_rejects_huge_timeout(self) -> None:
        tool = RunPythonTool(_FakeBackend(_OK))
        with pytest.raises(ToolError, match="invalid arguments"):
            tool.call({"code": "print(1)", "timeout_seconds": 10_000})

    def test_extra_field_rejected(self) -> None:
        tool = RunPythonTool(_FakeBackend(_OK))
        with pytest.raises(ToolError, match="invalid arguments"):
            tool.call({"code": "print(1)", "bogus": "x"})

    def test_arguments_forwarded_to_backend(self) -> None:
        backend = _FakeBackend(_OK)
        tool = RunPythonTool(backend)
        tool.call({"code": "print(1)", "timeout_seconds": 5.0, "stdin": "data"})
        assert backend.last_request is not None
        assert backend.last_request.code == "print(1)"
        assert backend.last_request.timeout_seconds == 5.0
        assert backend.last_request.stdin == "data"


class TestRunPythonEndToEnd:
    """Tool + LocalSubprocessBackend together — the production wiring."""

    def test_real_subprocess_round_trip(self) -> None:
        tool = RunPythonTool(LocalSubprocessBackend())
        result = tool.call({"code": "print(2 + 2)"})
        assert result["stdout"].strip() == "4"
        assert result["succeeded"] is True

    def test_satisfies_tool_protocol(self) -> None:
        tool = RunPythonTool(LocalSubprocessBackend())
        assert isinstance(tool, Tool)
        assert tool.name == "run_python"
        assert "input_schema" in dir(tool)

    def test_registers_into_registry(self) -> None:
        tool = RunPythonTool(LocalSubprocessBackend())
        reg = ToolRegistry([tool])
        assert "run_python" in reg
        result = reg.call("run_python", {"code": "print(1+1)"})
        assert result["stdout"].strip() == "2"

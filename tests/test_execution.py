"""Unit tests for the execution backend layer.

LocalSubprocessBackend is the only concrete backend right now. These tests
cover the hardening guarantees the backend is supposed to provide
(timeout, env scrub, fresh cwd, stream capture), not the K8s backend.
"""

import os
import sys
from pathlib import Path

import pytest

from harness_weaver.execution import (
    ExecutionRequest,
    ExecutionResult,
    LocalSubprocessBackend,
)


@pytest.fixture
def backend() -> LocalSubprocessBackend:
    return LocalSubprocessBackend()


class TestSucceededFlag:
    def test_zero_exit_and_no_timeout_means_succeeded(self) -> None:
        result = ExecutionResult(
            exit_code=0, stdout="", stderr="", timed_out=False, duration_seconds=0.1
        )
        assert result.succeeded is True

    def test_nonzero_exit_means_not_succeeded(self) -> None:
        result = ExecutionResult(
            exit_code=1, stdout="", stderr="", timed_out=False, duration_seconds=0.1
        )
        assert result.succeeded is False

    def test_timeout_means_not_succeeded(self) -> None:
        result = ExecutionResult(
            exit_code=0, stdout="", stderr="", timed_out=True, duration_seconds=1.0
        )
        assert result.succeeded is False


class TestLocalBackendBasics:
    def test_captures_stdout(self, backend: LocalSubprocessBackend) -> None:
        result = backend.run(ExecutionRequest(code="print('hello')"))
        assert result.exit_code == 0
        assert result.stdout.strip() == "hello"
        assert result.stderr == ""
        assert result.timed_out is False
        assert result.succeeded is True

    def test_captures_stderr(self, backend: LocalSubprocessBackend) -> None:
        code = "import sys; sys.stderr.write('boom\\n')"
        result = backend.run(ExecutionRequest(code=code))
        assert result.exit_code == 0
        assert "boom" in result.stderr

    def test_captures_nonzero_exit_code(self, backend: LocalSubprocessBackend) -> None:
        result = backend.run(ExecutionRequest(code="raise SystemExit(7)"))
        assert result.exit_code == 7
        assert result.succeeded is False

    def test_propagates_traceback_on_uncaught_exception(
        self, backend: LocalSubprocessBackend
    ) -> None:
        result = backend.run(ExecutionRequest(code="1/0"))
        assert result.exit_code != 0
        assert "ZeroDivisionError" in result.stderr

    def test_stdin_is_piped(self, backend: LocalSubprocessBackend) -> None:
        code = "import sys; print(sys.stdin.read().upper())"
        result = backend.run(ExecutionRequest(code=code, stdin="hello"))
        assert result.stdout.strip() == "HELLO"

    def test_duration_is_measured(self, backend: LocalSubprocessBackend) -> None:
        result = backend.run(
            ExecutionRequest(code="import time; time.sleep(0.1)", timeout_seconds=5.0)
        )
        assert result.duration_seconds >= 0.1
        assert result.duration_seconds < 5.0


class TestTimeoutEnforcement:
    def test_timeout_kills_long_running_process(self, backend: LocalSubprocessBackend) -> None:
        result = backend.run(
            ExecutionRequest(
                code="import time; time.sleep(5)",
                timeout_seconds=0.5,
            )
        )
        assert result.timed_out is True
        assert result.succeeded is False
        # Duration should be close to the timeout, not the sleep length.
        assert result.duration_seconds < 2.0

    def test_timeout_does_not_eat_pre_timeout_stdout(self, backend: LocalSubprocessBackend) -> None:
        # Print something, flush, then hang. We should still see the print.
        code = (
            "import sys, time\nsys.stdout.write('partial\\n'); sys.stdout.flush()\ntime.sleep(5)\n"
        )
        result = backend.run(ExecutionRequest(code=code, timeout_seconds=0.5))
        assert result.timed_out is True
        assert "partial" in result.stdout


class TestEnvironmentIsolation:
    def test_unrelated_env_vars_are_not_inherited(
        self, backend: LocalSubprocessBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Set a secret-shaped env var; the snippet must not see it.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-leaked")
        code = "import os\nprint(os.environ.get('ANTHROPIC_API_KEY', 'absent'))\n"
        result = backend.run(ExecutionRequest(code=code))
        assert result.stdout.strip() == "absent"

    def test_path_is_inherited(
        self, backend: LocalSubprocessBackend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # PATH must come through so the interpreter can find shared libs etc.
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
        code = "import os; print(len(os.environ.get('PATH', '')))"
        result = backend.run(ExecutionRequest(code=code))
        assert int(result.stdout.strip()) > 0


class TestWorkingDirectoryIsolation:
    def test_cwd_is_a_fresh_temp_dir(self, backend: LocalSubprocessBackend) -> None:
        code = (
            "import pathlib\n"
            "p = pathlib.Path.cwd()\n"
            "print(p)\n"
            "print(sorted(c.name for c in p.iterdir()))\n"
        )
        result = backend.run(ExecutionRequest(code=code))
        assert result.exit_code == 0
        lines = result.stdout.strip().splitlines()
        assert "hw-sandbox" in lines[0]  # our prefix
        # The temp dir contains only our snippet.py
        assert lines[1] == "['snippet.py']"

    def test_cwd_is_cleaned_up_after_run(self, backend: LocalSubprocessBackend) -> None:
        code = "import pathlib; print(pathlib.Path.cwd())"
        result = backend.run(ExecutionRequest(code=code))
        cwd_path = result.stdout.strip()
        # Directory should be gone now.
        assert not Path(cwd_path).exists()


class TestPythonExecutableSelection:
    def test_uses_current_interpreter_by_default(self, backend: LocalSubprocessBackend) -> None:
        # The child should report the same major.minor as our interpreter.
        code = "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        result = backend.run(ExecutionRequest(code=code))
        expected = f"{sys.version_info.major}.{sys.version_info.minor}"
        assert result.stdout.strip() == expected

    def test_explicit_python_executable(self) -> None:
        backend = LocalSubprocessBackend(python_executable=sys.executable)
        result = backend.run(ExecutionRequest(code="print(1+1)"))
        assert result.stdout.strip() == "2"

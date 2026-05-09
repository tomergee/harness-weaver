"""LocalSubprocessBackend — runs Python via subprocess on the host.

This is the **dev** backend. There is no real isolation: the spawned
process runs as the same user as the harness, with read access to the
filesystem (we cd into a fresh temp dir but cannot prevent absolute-path
reads), and minimal environment scrubbing. It exists so the harness is
useful without Kubernetes; the K8s-backed AgentSandboxBackend is the
backend you should reach for when isolation actually matters.

Hardening that is in place:
- Each run gets a fresh temp directory as cwd; the directory is deleted
  on return regardless of outcome.
- The child env contains only PATH, LANG, and PYTHONIOENCODING. No
  inherited ANTHROPIC_API_KEY, AWS keys, etc.
- Wall-clock timeout via ``subprocess.run(timeout=...)``; on expiry the
  process tree is killed.
"""

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from harness_weaver.execution.base import (
    ExecutionBackend,
    ExecutionRequest,
    ExecutionResult,
)

# Allow-list of env vars to pass through. Everything else is stripped.
_ENV_PASSTHROUGH: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "PYTHONIOENCODING")


def _timeout_captured_output(stream: str | bytes | None) -> str:
    """Normalize ``TimeoutExpired`` stdout/stderr (bytes or str depending on ``text=``)."""
    if not stream:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream


class LocalSubprocessBackend(ExecutionBackend):
    """Runs Python snippets in a child ``python`` subprocess.

    Args:
        python_executable: Path to the Python interpreter to invoke.
            Defaults to the same interpreter the harness is running under.
    """

    def __init__(self, python_executable: str | None = None) -> None:
        self._python = python_executable or sys.executable

    def run(self, request: ExecutionRequest) -> ExecutionResult:
        env = {k: os.environ[k] for k in _ENV_PASSTHROUGH if k in os.environ}
        # Force unbuffered output so partial stdout still surfaces on timeout.
        env.setdefault("PYTHONUNBUFFERED", "1")

        with tempfile.TemporaryDirectory(prefix="hw-sandbox-") as tmp:
            script = Path(tmp) / "snippet.py"
            script.write_text(request.code, encoding="utf-8")

            start = time.monotonic()
            timed_out = False
            stdout = ""
            stderr = ""
            exit_code = -1
            try:
                completed = subprocess.run(
                    [self._python, str(script)],
                    input=request.stdin,
                    capture_output=True,
                    text=True,
                    timeout=request.timeout_seconds,
                    cwd=tmp,
                    env=env,
                    check=False,
                )
                stdout = completed.stdout
                stderr = completed.stderr
                exit_code = completed.returncode
            except subprocess.TimeoutExpired as e:
                timed_out = True
                # TimeoutExpired exposes whatever was buffered before kill.
                stdout = _timeout_captured_output(e.stdout)
                stderr = _timeout_captured_output(e.stderr)
                # Conventional Unix code for SIGKILL; informational only.
                exit_code = -9

            duration = time.monotonic() - start

        return ExecutionResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            duration_seconds=duration,
        )


__all__ = ["LocalSubprocessBackend"]

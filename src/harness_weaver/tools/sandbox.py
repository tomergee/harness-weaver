"""run_python tool: executes a snippet via an :class:`ExecutionBackend`.

The tool is a thin wrapper. All sandboxing, timeout, and stream capture
happens in the backend, which is injected at construction time. The same
tool can sit on top of LocalSubprocessBackend (dev) or a K8s-backed
sandbox in production.
"""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from harness_weaver.execution import ExecutionBackend, ExecutionRequest
from harness_weaver.tools.base import Tool


class RunPythonInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        description=(
            "Python source to execute. Runs in a fresh process with no inherited "
            "environment beyond PATH; treat the working directory as ephemeral. "
            "Print results to stdout — there is no return value channel."
        ),
        min_length=1,
    )
    timeout_seconds: float = Field(
        default=30.0,
        gt=0.0,
        le=300.0,
        description="Wall-clock timeout. The process is killed when this expires.",
    )
    stdin: str | None = Field(
        default=None,
        description="Optional text to pipe into the snippet's stdin.",
    )


class RunPythonOutput(BaseModel):
    exit_code: int = Field(description="Process exit code; -9 indicates a timeout kill.")
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float
    succeeded: bool = Field(description="True iff exit_code == 0 and not timed_out.")


class RunPythonTool(Tool[RunPythonInput, RunPythonOutput]):
    name: ClassVar[str] = "run_python"
    description: ClassVar[str] = (
        "Execute a Python snippet in a sandboxed subprocess and return its stdout, "
        "stderr, exit code, and wall-clock duration. Use for filtering, sorting, "
        "or numeric reasoning over data the catalog tools have returned. The "
        "snippet has no network access and a fresh working directory; print "
        "results to stdout to make them readable."
    )
    input_model = RunPythonInput
    output_model = RunPythonOutput

    def __init__(self, backend: ExecutionBackend) -> None:
        self._backend = backend

    def execute(self, args: RunPythonInput) -> RunPythonOutput:
        result = self._backend.run(
            ExecutionRequest(
                code=args.code,
                timeout_seconds=args.timeout_seconds,
                stdin=args.stdin,
            )
        )
        return RunPythonOutput(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
            duration_seconds=result.duration_seconds,
            succeeded=result.succeeded,
        )


__all__ = ["RunPythonInput", "RunPythonOutput", "RunPythonTool"]

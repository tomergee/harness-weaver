"""Harness: compiles a Configuration, runs a Task, returns a Trajectory.

The Harness owns the lifecycle. It does *not* know about Anthropic, MCP,
or Kubernetes — the :class:`AgentRunner` does. That seam is what lets us
build an e2e test against a scripted fake runner without an API key, and
swap in the real SDK-backed runner in production.

Single entry point: :meth:`Harness.run`. Everything else (registry
construction from a configuration, execution backend wiring, scoping a
worker's tool surface) is internal plumbing the caller doesn't see.
"""

from harness_weaver.agent_runner import AgentRunner
from harness_weaver.catalog import Catalog
from harness_weaver.configurations import Configuration
from harness_weaver.execution import ExecutionBackend, LocalSubprocessBackend
from harness_weaver.task import Task
from harness_weaver.tools import (
    GetMetadataTool,
    RunPythonTool,
    SearchTitlesTool,
    ToolRegistry,
    UserHistoryTool,
)
from harness_weaver.trajectory import Trajectory


class Harness:
    """Single-instance experiment runner.

    Args:
        catalog: The data layer the catalog tools sit on top of.
        runner: Strategy for actually running the agent session.
        execution_backend: Backend for the ``run_python`` tool, if the
            configuration uses it. Defaults to a fresh
            :class:`LocalSubprocessBackend`. Pass an explicit backend for
            tests or to share one across runs.
    """

    def __init__(
        self,
        *,
        catalog: Catalog,
        runner: AgentRunner,
        execution_backend: ExecutionBackend | None = None,
    ) -> None:
        self._catalog = catalog
        self._runner = runner
        self._execution_backend = execution_backend or LocalSubprocessBackend()

    def run(self, task: Task, configuration: Configuration) -> Trajectory:
        """Execute one (task, configuration) pair end-to-end."""
        registry = self._build_registry(configuration)
        prompt = self._compose_prompt(task)
        trajectory = self._runner.run(
            prompt=prompt,
            configuration=configuration,
            registry=registry,
            task_id=task.task_id,
        )
        # If the execution backend has sandbox telemetry to share (only
        # AgentSandboxBackend does, and only when at least one
        # run_python call provisioned the pod), stamp it on the
        # trajectory. Trajectory is frozen, so we model_copy.
        telemetry_fn = getattr(self._execution_backend, "telemetry", None)
        if callable(telemetry_fn):
            telemetry = telemetry_fn()
            if telemetry is not None:
                trajectory = trajectory.model_copy(update={"sandbox_telemetry": telemetry})
        return trajectory

    def _build_registry(self, configuration: Configuration) -> ToolRegistry:
        """Construct a ToolRegistry containing every tool any agent in the
        configuration might need.

        We register a superset (the union of orchestrator + workers' allowed
        tools); per-agent allow-list enforcement happens inside the runner
        when it observes a tool call. This keeps the registry simple and
        avoids duplicating tool instances.
        """
        wanted: set[str] = set(configuration.allowed_tools)
        for agent in configuration.agents:
            wanted |= set(agent.allowed_tools)

        registry = ToolRegistry()
        # Catalog tools share the catalog instance; cheap to construct.
        if "search_titles" in wanted:
            registry.register(SearchTitlesTool(self._catalog))
        if "get_metadata" in wanted:
            registry.register(GetMetadataTool(self._catalog))
        if "user_history" in wanted:
            registry.register(UserHistoryTool(self._catalog))
        if "run_python" in wanted:
            registry.register(RunPythonTool(self._execution_backend))
        return registry

    @staticmethod
    def _compose_prompt(task: Task) -> str:
        """Combine the user prompt with optional context fields.

        Currently a thin wrapper that appends ``user_id`` if present, since
        ``user_history`` needs it. More elaborate composition (e.g. system
        message templating) is the configuration's job, not the harness's.
        """
        if task.user_id is None:
            return task.user_prompt
        return f"{task.user_prompt}\n\n[user_id={task.user_id}]"


__all__ = ["Harness"]

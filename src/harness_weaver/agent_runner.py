"""AgentRunner: the seam between Harness and the actual LLM session.

A runner takes a prompt, a configuration, and a tool registry, and produces
a :class:`Trajectory`. Two implementations:

* :class:`RealAgentRunner` — wraps ``claude-agent-sdk.query`` and an
  in-process MCP server exposing the registry. Live model in the loop;
  needs ``ANTHROPIC_API_KEY``.
* :class:`FakeAgentRunner` — replays a scripted sequence of decisions but
  invokes the *real* tool registry, so tests exercise the registry/tool
  integration without an API key.

Configurations and the registry interplay deliberately: ``allowed_tools``
on a configuration is enforced inside the runner, so a worker that asks
for a tool it isn't allowed to call gets a structured error rather than
silent execution.
"""

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal

import claude_agent_sdk as sdk

from harness_weaver.configurations import ORCHESTRATOR_AGENT_ID, Configuration
from harness_weaver.mcp_server import build_sdk_server
from harness_weaver.sdk_compile import compile_options
from harness_weaver.sdk_translate import SdkMessageTranslator
from harness_weaver.tools import ToolError, ToolRegistry
from harness_weaver.trajectory import Trajectory, TrajectoryRecorder

QueryFn = Callable[..., AsyncIterator[Any]]
"""Signature of ``claude_agent_sdk.query`` — kept open so tests can inject a fake."""


class AgentRunner(ABC):
    """Strategy for actually running an agent session."""

    @abstractmethod
    def run(
        self,
        *,
        prompt: str,
        configuration: Configuration,
        registry: ToolRegistry,
        task_id: str,
    ) -> Trajectory:
        """Execute one task end-to-end and return a populated Trajectory."""


# --- Real (production) runner -------------------------------------------


class RealAgentRunner(AgentRunner):
    """Production runner: drives ``claude_agent_sdk.query`` against an
    in-process MCP server wrapping our tool registry.

    Args:
        query_fn: Override the SDK's ``query`` function — used by tests
            to inject a scripted async iterator of SDK messages without
            hitting the API. Defaults to ``claude_agent_sdk.query``.

    The flow:
        1. Build an SDK MCP server from the registry.
        2. Compile the Configuration to ``ClaudeAgentOptions`` referencing
           that server.
        3. Run ``query()`` inside an asyncio loop, streaming messages.
        4. Translate each message to Trajectory events via
           :class:`SdkMessageTranslator`.

    Recording a vcrpy cassette of one live run lets CI replay the same
    trajectory deterministically; see ``tests/test_real_agent_runner.py``
    for the cassette hook.
    """

    def __init__(self, *, query_fn: QueryFn | None = None) -> None:
        self._query_fn: QueryFn = query_fn or sdk.query

    def run(
        self,
        *,
        prompt: str,
        configuration: Configuration,
        registry: ToolRegistry,
        task_id: str,
    ) -> Trajectory:
        recorder = TrajectoryRecorder(
            task_id=task_id,
            configuration_name=configuration.name,
        )
        recorder.user_message(prompt)

        mcp_server = build_sdk_server(registry)
        options = compile_options(configuration, mcp_server=mcp_server)
        translator = SdkMessageTranslator()

        async def drive() -> None:
            async for message in self._query_fn(prompt=prompt, options=options):
                translator.translate(message, recorder)

        asyncio.run(drive())
        return recorder.finish()


# --- Fake runner for tests -----------------------------------------------


@dataclass(frozen=True)
class _Say:
    text: str
    agent_id: str = ORCHESTRATOR_AGENT_ID
    kind: Literal["say"] = "say"


@dataclass(frozen=True)
class _Call:
    tool: str
    arguments: dict[str, Any]
    agent_id: str = ORCHESTRATOR_AGENT_ID
    kind: Literal["call"] = "call"


@dataclass(frozen=True)
class _Answer:
    text: str
    agent_id: str = ORCHESTRATOR_AGENT_ID
    kind: Literal["answer"] = "answer"


ScriptStep = _Say | _Call | _Answer


def say(text: str, *, agent_id: str = ORCHESTRATOR_AGENT_ID) -> _Say:
    """Script a piece of assistant text."""
    return _Say(text=text, agent_id=agent_id)


def call(
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    agent_id: str = ORCHESTRATOR_AGENT_ID,
) -> _Call:
    """Script a tool call. The registry actually executes it."""
    return _Call(tool=tool, arguments=arguments or {}, agent_id=agent_id)


def answer(text: str, *, agent_id: str = ORCHESTRATOR_AGENT_ID) -> _Answer:
    """Script the final answer that terminates the run."""
    return _Answer(text=text, agent_id=agent_id)


class FakeAgentRunner(AgentRunner):
    """Replays a fixed script while delegating tool execution to the real registry.

    Use this for unit and e2e tests of the harness. The script captures the
    *agent's* decisions; the *tools'* responses come from running the real
    registry, so the integration between Harness, registry, and tools is
    actually exercised.

    ``allowed_tools`` is enforced: a script that calls a tool the
    configuration didn't allow gets a ToolResult with an ``error`` field set,
    which is exactly how the real runner should behave.
    """

    def __init__(self, script: list[ScriptStep]) -> None:
        self._script = list(script)

    def run(
        self,
        *,
        prompt: str,
        configuration: Configuration,
        registry: ToolRegistry,
        task_id: str,
    ) -> Trajectory:
        recorder = TrajectoryRecorder(
            task_id=task_id,
            configuration_name=configuration.name,
        )
        recorder.user_message(prompt)
        allowed_per_agent = self._build_allowlist(configuration)

        for step in self._script:
            if isinstance(step, _Say):
                recorder.assistant_turn(step.text, agent_id=step.agent_id)
            elif isinstance(step, _Call):
                self._handle_call(step, registry, allowed_per_agent, recorder)
            else:  # _Answer
                recorder.final_answer(step.text, agent_id=step.agent_id)

        return recorder.finish()

    @staticmethod
    def _build_allowlist(configuration: Configuration) -> dict[str, frozenset[str]]:
        # Configuration.model_validator guarantees no agent uses
        # ORCHESTRATOR_AGENT_ID and that role names are unique, so this map
        # has no risk of silent overwrite.
        out: dict[str, frozenset[str]] = {
            ORCHESTRATOR_AGENT_ID: frozenset(configuration.allowed_tools)
        }
        for agent in configuration.agents:
            out[agent.role_name] = frozenset(agent.allowed_tools)
        return out

    @staticmethod
    def _handle_call(
        step: _Call,
        registry: ToolRegistry,
        allowed_per_agent: dict[str, frozenset[str]],
        recorder: TrajectoryRecorder,
    ) -> None:
        recorder.tool_use(step.tool, step.arguments, agent_id=step.agent_id)
        # Configuration-level allow-list check: the agent attempted a tool
        # it isn't supposed to be able to invoke. Surface as an error in the
        # trajectory rather than executing.
        allowed = allowed_per_agent.get(step.agent_id, frozenset())
        if step.tool not in allowed:
            recorder.tool_result(
                step.tool,
                error=(
                    f"agent {step.agent_id!r} is not permitted to call {step.tool!r}; "
                    f"allowed: {sorted(allowed)}"
                ),
                duration_seconds=0.0,
                agent_id=step.agent_id,
            )
            return

        start = time.monotonic()
        try:
            result = registry.call(step.tool, step.arguments)
        except (ToolError, KeyError) as exc:
            recorder.tool_result(
                step.tool,
                error=str(exc),
                duration_seconds=time.monotonic() - start,
                agent_id=step.agent_id,
            )
            return
        recorder.tool_result(
            step.tool,
            result=result,
            duration_seconds=time.monotonic() - start,
            agent_id=step.agent_id,
        )


__all__ = [
    "AgentRunner",
    "FakeAgentRunner",
    "RealAgentRunner",
    "ScriptStep",
    "answer",
    "call",
    "say",
]

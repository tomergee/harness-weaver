"""End-to-end test for :class:`RealAgentRunner` with an injected fake SDK.

We don't have an Anthropic API key in CI, so the live ``query()`` is
replaced with a scripted async iterator that produces SDK message
objects. This exercises the *whole* RealAgentRunner pipeline:

* Configuration → ClaudeAgentOptions compilation
* in-process MCP server build from the registry
* asyncio loop driving the (fake) query
* SDK message → Trajectory event translation
* recorder finalization

The only thing not exercised is talking to Anthropic. When you record a
vcrpy cassette with a real key, swap the fake out for the real
``claude_agent_sdk.query`` and the cassette will replay deterministically.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import claude_agent_sdk as sdk
import pytest

from harness_weaver.agent_runner import HarnessRunError, RealAgentRunner
from harness_weaver.catalog import Catalog
from harness_weaver.configurations import SINGLE_AGENT_BASIC
from harness_weaver.harness import Harness
from harness_weaver.task import Task
from harness_weaver.tools import ToolRegistry
from harness_weaver.trajectory import (
    AssistantTurn,
    FinalAnswer,
    ToolResult,
    ToolUse,
)


@pytest.fixture(scope="module")
def catalog() -> Catalog:
    return Catalog.load_default()


@pytest.fixture
def discovery_task() -> Task:
    return Task(
        task_id="discovery-mood-tense",
        user_prompt="Find me a tense thriller under two hours.",
        user_id="user-001",
    )


def _assistant_msg(*content: object) -> sdk.AssistantMessage:
    return sdk.AssistantMessage(
        content=list(content),  # type: ignore[arg-type]
        model="test-model",
        parent_tool_use_id=None,
        error=None,
        usage=None,
        message_id=None,
        stop_reason=None,
        session_id=None,
        uuid=None,
    )


def _result_msg(text: str) -> sdk.ResultMessage:
    return sdk.ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=8,
        is_error=False,
        num_turns=1,
        session_id="s1",
        stop_reason="end_turn",
        total_cost_usd=0.0,
        usage=None,
        result=text,
        structured_output=None,
        model_usage=None,
        permission_denials=None,
        deferred_tool_use=None,
        errors=None,
        api_error_status=None,
        uuid=None,
    )


def _make_query_fn(messages: list[object]) -> Any:
    """Build a fake ``query_fn`` that yields the given SDK messages.

    Records the (prompt, options) it was called with on attributes so
    tests can verify that the harness compiled and passed them correctly.
    """
    captured: dict[str, Any] = {}

    async def fake_query(*, prompt: str, options: sdk.ClaudeAgentOptions) -> AsyncIterator[Any]:
        captured["prompt"] = prompt
        captured["options"] = options
        for msg in messages:
            yield msg

    fake_query.captured = captured  # type: ignore[attr-defined]
    return fake_query


class TestRealAgentRunnerEndToEnd:
    def test_full_round_trip_with_fake_query(self, catalog: Catalog, discovery_task: Task) -> None:
        # Script the SDK output: one tool call (with its result) then a final answer.
        messages = [
            _assistant_msg(
                sdk.TextBlock(text="Searching for thrillers under 120 minutes."),
                sdk.ToolUseBlock(
                    id="t1",
                    name="search_titles",
                    input={"genres": ["Thriller"], "max_runtime": 120, "limit": 3},
                ),
            ),
            _assistant_msg(
                sdk.ToolResultBlock(
                    tool_use_id="t1",
                    content=[
                        {
                            "type": "text",
                            "text": '{"hits":[{"id":"m016","title":"Memento"}],"total_matched":1}',
                        }
                    ],
                    is_error=False,
                )
            ),
            _result_msg("I'd recommend Memento — taut, 113 minutes, twisty thriller."),
        ]
        fake = _make_query_fn(messages)
        harness = Harness(catalog=catalog, runner=RealAgentRunner(query_fn=fake))
        trajectory = harness.run(discovery_task, SINGLE_AGENT_BASIC)

        assert trajectory.task_id == "discovery-mood-tense"
        assert trajectory.configuration_name == "single-agent-basic"
        assert trajectory.final_answer is not None
        assert "Memento" in trajectory.final_answer

        # Event sequence: user_message, assistant_turn, tool_use, tool_result, final_answer
        assert trajectory.event_types() == [
            "user_message",
            "assistant_turn",
            "tool_use",
            "tool_result",
            "final_answer",
        ]

        # Tool call shape preserved.
        tool_use = next(e for e in trajectory.events if isinstance(e, ToolUse))
        assert tool_use.tool_name == "search_titles"
        assert tool_use.arguments["max_runtime"] == 120

        # Tool result parsed from MCP text payload back into a dict.
        tool_result = next(e for e in trajectory.events if isinstance(e, ToolResult))
        assert tool_result.error is None
        assert tool_result.result == {
            "hits": [{"id": "m016", "title": "Memento"}],
            "total_matched": 1,
        }
        # Name resolved via the use-id mapping built up by the translator.
        assert tool_result.tool_name == "search_titles"

    def test_options_are_compiled_correctly(self, catalog: Catalog, discovery_task: Task) -> None:
        fake = _make_query_fn([_result_msg("done")])
        harness = Harness(catalog=catalog, runner=RealAgentRunner(query_fn=fake))
        harness.run(discovery_task, SINGLE_AGENT_BASIC)

        captured = fake.captured  # type: ignore[attr-defined]
        # Prompt was the user_prompt with [user_id=...] appended.
        assert captured["prompt"].startswith("Find me a tense thriller")
        assert "[user_id=user-001]" in captured["prompt"]

        opts: sdk.ClaudeAgentOptions = captured["options"]
        assert opts.system_prompt == SINGLE_AGENT_BASIC.system_prompt
        # allowed_tools carries the MCP-qualified names so the SDK auto-permits.
        # Compared as sets because the compiler sorts/dedupes for determinism.
        assert set(opts.allowed_tools) == {
            f"mcp__harness_weaver__{t}" for t in SINGLE_AGENT_BASIC.allowed_tools
        }
        # MCP server registered under the default name.
        assert isinstance(opts.mcp_servers, dict)
        assert "harness_weaver" in opts.mcp_servers

    def test_assistant_text_blocks_recorded_as_turns(
        self, catalog: Catalog, discovery_task: Task
    ) -> None:
        messages = [
            _assistant_msg(sdk.TextBlock(text="thinking")),
            _assistant_msg(sdk.TextBlock(text="more thinking")),
            _result_msg("final"),
        ]
        fake = _make_query_fn(messages)
        harness = Harness(catalog=catalog, runner=RealAgentRunner(query_fn=fake))
        trajectory = harness.run(discovery_task, SINGLE_AGENT_BASIC)

        turns = [e for e in trajectory.events if isinstance(e, AssistantTurn)]
        assert [t.text for t in turns] == ["thinking", "more thinking"]
        assert isinstance(trajectory.events[-1], FinalAnswer)


class TestRunArunSplit:
    """``run`` is sync; ``arun`` is the async fundamental. Calling ``run``
    from inside a running loop should raise a clear error pointing at
    ``arun`` (gemini-code-assist review on PR #3)."""

    def test_run_in_sync_context_works(self, catalog: Catalog, discovery_task: Task) -> None:
        fake = _make_query_fn([_result_msg("done")])
        runner = RealAgentRunner(query_fn=fake)
        registry = ToolRegistry()
        traj = runner.run(
            prompt="ping",
            configuration=SINGLE_AGENT_BASIC,
            registry=registry,
            task_id="t1",
        )
        assert traj.task_id == "t1"
        assert traj.final_answer == "done"

    def test_run_from_running_loop_raises_with_actionable_message(
        self, catalog: Catalog, discovery_task: Task
    ) -> None:
        fake = _make_query_fn([_result_msg("done")])
        runner = RealAgentRunner(query_fn=fake)
        registry = ToolRegistry()

        async def call_run_from_inside_loop() -> None:
            runner.run(
                prompt="ping",
                configuration=SINGLE_AGENT_BASIC,
                registry=registry,
                task_id="t1",
            )

        with pytest.raises(RuntimeError, match="arun"):
            asyncio.run(call_run_from_inside_loop())

    def test_arun_works_in_async_context(self, catalog: Catalog, discovery_task: Task) -> None:
        fake = _make_query_fn([_result_msg("done from arun")])
        runner = RealAgentRunner(query_fn=fake)
        registry = ToolRegistry()

        async def use_arun() -> None:
            traj = await runner.arun(
                prompt="ping",
                configuration=SINGLE_AGENT_BASIC,
                registry=registry,
                task_id="t1",
            )
            assert traj.final_answer == "done from arun"

        asyncio.run(use_arun())


class TestPartialTrajectoryOnError:
    """When the SDK raises mid-flight, the partial Trajectory must survive
    so callers can debug what the agent did before the failure
    (gemini-code-assist review on PR #3)."""

    def test_exception_in_query_raises_HarnessRunError_with_partial(
        self, catalog: Catalog, discovery_task: Task
    ) -> None:
        async def buggy_query(
            *, prompt: str, options: sdk.ClaudeAgentOptions
        ) -> AsyncIterator[Any]:
            del prompt, options
            yield _assistant_msg(sdk.TextBlock(text="step one"))
            yield _assistant_msg(sdk.ToolUseBlock(id="t1", name="search_titles", input={}))
            raise RuntimeError("simulated mid-run failure")

        runner = RealAgentRunner(query_fn=buggy_query)
        harness = Harness(catalog=catalog, runner=runner)
        with pytest.raises(HarnessRunError) as excinfo:
            harness.run(discovery_task, SINGLE_AGENT_BASIC)

        err = excinfo.value
        # The original exception is chained for traceback inspection.
        assert isinstance(err.__cause__, RuntimeError)
        assert "simulated mid-run failure" in str(err.__cause__)
        # The partial trajectory recorded everything before the failure ...
        assert err.partial_trajectory.task_id == "discovery-mood-tense"
        assert any(
            isinstance(e, AssistantTurn) and "step one" in e.text
            for e in err.partial_trajectory.events
        )
        assert err.partial_trajectory.tool_call_count == 1
        # ... plus a synthetic final assistant_turn marking the failure.
        last_event = err.partial_trajectory.events[-1]
        assert isinstance(last_event, AssistantTurn)
        assert "<run aborted" in last_event.text
        assert "simulated mid-run failure" in last_event.text


class TestCustomServerName:
    """``server_name`` flows from the runner through compile_options and
    the translator, so a non-default name is consistent end-to-end
    (gemini-code-assist review on PR #3)."""

    def test_custom_server_name_threaded_through(
        self, catalog: Catalog, discovery_task: Task
    ) -> None:
        captured: dict[str, Any] = {}

        async def query_capturing_options(
            *, prompt: str, options: sdk.ClaudeAgentOptions
        ) -> AsyncIterator[Any]:
            captured["options"] = options
            yield _result_msg("done")

        runner = RealAgentRunner(query_fn=query_capturing_options, server_name="alt_srv")
        harness = Harness(catalog=catalog, runner=runner)
        harness.run(discovery_task, SINGLE_AGENT_BASIC)

        opts: sdk.ClaudeAgentOptions = captured["options"]
        # MCP server registered under the custom name (not the default).
        assert isinstance(opts.mcp_servers, dict)
        assert "alt_srv" in opts.mcp_servers
        assert "harness_weaver" not in opts.mcp_servers
        # Allow-list uses the custom-prefixed names.
        assert any(t.startswith("mcp__alt_srv__") for t in opts.allowed_tools)

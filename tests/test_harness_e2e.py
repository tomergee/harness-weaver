"""End-to-end harness tests using a scripted FakeAgentRunner.

These exercise the *whole* path from Task in to Trajectory out: registry
construction from a Configuration, prompt composition, allow-list
enforcement per agent, and the recorder writing a discriminated-union
trajectory that round-trips through JSON.

The fake runner replays scripted decisions but invokes the *real* tool
registry, so tools, the catalog, and the registry are all under test
here. Only the LLM is stubbed.

When a real Anthropic API key is wired up, swap ``FakeAgentRunner`` for
``RealAgentRunner`` (or record a vcrpy cassette and replay it via the
real runner). The Harness signature does not change.
"""

import pytest

from harness_weaver.agent_runner import FakeAgentRunner, RealAgentRunner, answer, call, say
from harness_weaver.catalog import Catalog
from harness_weaver.configurations import (
    MULTI_AGENT_DISCOVERY_EXPLAINER,
    SINGLE_AGENT_BASIC,
    SINGLE_AGENT_WITH_SANDBOX,
)
from harness_weaver.harness import Harness
from harness_weaver.task import Task
from harness_weaver.trajectory import ToolResult, ToolUse


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


# --- single-agent path ---------------------------------------------------


class TestSingleAgentBasic:
    def test_full_round_trip(self, catalog: Catalog, discovery_task: Task) -> None:
        runner = FakeAgentRunner(
            [
                say("Looking up the user's history first."),
                call("user_history", {"user_id": "user-001", "limit": 5}),
                say("Now searching for tense thrillers under 120 minutes."),
                call(
                    "search_titles",
                    {
                        "genres": ["Thriller"],
                        "max_runtime": 120,
                        "min_rating": 7.5,
                        "limit": 5,
                    },
                ),
                answer("I recommend Memento — 113 min, rating 8.4, a tense memory-thriller."),
            ]
        )
        harness = Harness(catalog=catalog, runner=runner)
        trajectory = harness.run(discovery_task, SINGLE_AGENT_BASIC)

        assert trajectory.task_id == "discovery-mood-tense"
        assert trajectory.configuration_name == "single-agent-basic"
        assert trajectory.tool_call_count == 2
        assert trajectory.final_answer is not None
        assert "Memento" in trajectory.final_answer

        # Shape: user_msg, say, call, result, say, call, result, answer.
        assert trajectory.event_types() == [
            "user_message",
            "assistant_turn",
            "tool_use",
            "tool_result",
            "assistant_turn",
            "tool_use",
            "tool_result",
            "final_answer",
        ]

        # Tool results came from the real registry, not the fake.
        first_call = trajectory.tool_calls[0]
        assert first_call.tool_name == "user_history"
        first_result = trajectory.events[3]
        assert isinstance(first_result, ToolResult)
        assert first_result.error is None
        assert first_result.result is not None
        # user-001 has 10 thriller-heavy ratings; limited to 5 here.
        assert len(first_result.result["entries"]) == 5
        assert first_result.result["total_events"] == 10

    def test_round_trip_through_json(self, catalog: Catalog, discovery_task: Task) -> None:
        runner = FakeAgentRunner(
            [
                call("search_titles", {"genres": ["Drama"], "limit": 1}),
                answer("Done."),
            ]
        )
        trajectory = Harness(catalog=catalog, runner=runner).run(discovery_task, SINGLE_AGENT_BASIC)
        from harness_weaver.trajectory import Trajectory

        restored = Trajectory.model_validate_json(trajectory.model_dump_json())
        assert restored == trajectory


class TestAllowListEnforcement:
    def test_calling_disallowed_tool_records_error(
        self, catalog: Catalog, discovery_task: Task
    ) -> None:
        # SINGLE_AGENT_BASIC does not allow run_python.
        runner = FakeAgentRunner(
            [
                call("run_python", {"code": "print('nope')"}),
                answer("attempted"),
            ]
        )
        trajectory = Harness(catalog=catalog, runner=runner).run(discovery_task, SINGLE_AGENT_BASIC)
        result_event = next(e for e in trajectory.events if isinstance(e, ToolResult))
        assert result_event.error is not None
        assert "not permitted" in result_event.error
        assert result_event.result is None

    def test_unknown_tool_in_script_records_error(
        self, catalog: Catalog, discovery_task: Task
    ) -> None:
        runner = FakeAgentRunner(
            [
                call("nonexistent_tool", {}),
                answer("nope"),
            ]
        )
        # Scope "nonexistent_tool" into allowed_tools so the allow-list passes;
        # the registry should then raise KeyError, which the runner captures.
        from harness_weaver.configurations import Configuration

        permissive = Configuration(
            name="permissive",
            description="for the test",
            system_prompt="x",
            allowed_tools=("nonexistent_tool", "search_titles"),
        )
        trajectory = Harness(catalog=catalog, runner=runner).run(discovery_task, permissive)
        result_event = next(e for e in trajectory.events if isinstance(e, ToolResult))
        assert result_event.error is not None
        assert "no tool named" in result_event.error


class TestSingleAgentWithSandbox:
    def test_run_python_executed_in_subprocess(
        self, catalog: Catalog, discovery_task: Task
    ) -> None:
        runner = FakeAgentRunner(
            [
                call(
                    "run_python",
                    {"code": "print(sum(range(101)))"},  # 5050
                ),
                answer("Sum is 5050."),
            ]
        )
        trajectory = Harness(catalog=catalog, runner=runner).run(
            discovery_task, SINGLE_AGENT_WITH_SANDBOX
        )
        result_event = next(e for e in trajectory.events if isinstance(e, ToolResult))
        assert result_event.error is None
        assert result_event.result is not None
        assert result_event.result["stdout"].strip() == "5050"
        assert result_event.result["succeeded"] is True


# --- multi-agent path ----------------------------------------------------


class TestMultiAgentDiscoveryExplainer:
    def test_workers_have_distinct_tool_surfaces(
        self, catalog: Catalog, discovery_task: Task
    ) -> None:
        runner = FakeAgentRunner(
            [
                say("Delegating discovery to the Discovery worker.", agent_id="orchestrator"),
                # Discovery worker can call search_titles
                call(
                    "search_titles",
                    {"genres": ["Thriller"], "max_runtime": 120, "limit": 3},
                    agent_id="discovery",
                ),
                say("Now asking Explainer to justify.", agent_id="orchestrator"),
                # Explainer can only call get_metadata
                call("get_metadata", {"movie_id": "m016"}, agent_id="explainer"),
                # Explainer trying search_titles should be blocked
                call(
                    "search_titles",
                    {"genres": ["Thriller"]},
                    agent_id="explainer",
                ),
                answer("Recommended: Memento."),
            ]
        )
        trajectory = Harness(catalog=catalog, runner=runner).run(
            discovery_task, MULTI_AGENT_DISCOVERY_EXPLAINER
        )

        # All three tool calls are recorded.
        tool_uses = [e for e in trajectory.events if isinstance(e, ToolUse)]
        assert [(t.tool_name, t.agent_id) for t in tool_uses] == [
            ("search_titles", "discovery"),
            ("get_metadata", "explainer"),
            ("search_titles", "explainer"),
        ]

        # The third call (explainer → search_titles) is blocked by the
        # configuration's allow-list and surfaces as a ToolResult error.
        results = [e for e in trajectory.events if isinstance(e, ToolResult)]
        assert results[0].error is None  # discovery's search_titles is allowed
        assert results[1].error is None  # explainer's get_metadata is allowed
        assert results[2].error is not None  # explainer's search_titles is not
        assert "not permitted" in results[2].error
        assert "explainer" in results[2].error


# --- RealAgentRunner --------------------------------------------------


class TestRealAgentRunnerStub:
    def test_raises_with_actionable_message(self, catalog: Catalog, discovery_task: Task) -> None:
        # The stub exists so the architecture is in place; calling it should
        # tell you exactly what's missing rather than fail mysteriously.
        harness = Harness(catalog=catalog, runner=RealAgentRunner())
        with pytest.raises(NotImplementedError, match="SDK-wiring PR"):
            harness.run(discovery_task, SINGLE_AGENT_BASIC)

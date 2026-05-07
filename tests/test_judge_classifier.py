"""Unit tests for the rules-based failure-mode classifier."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from harness_weaver.judge.classifier import FailureMode, classify
from harness_weaver.trajectory import (
    AssistantTurn,
    FinalAnswer,
    ToolResult,
    ToolUse,
    Trajectory,
    UserMessage,
)


def _ts() -> datetime:
    return datetime(2025, 1, 1, tzinfo=UTC)


def _trajectory(*events: object, final_answer: str | None = None) -> Trajectory:
    return Trajectory(
        task_id="t1",
        configuration_name="cfg",
        started_at=_ts(),
        completed_at=_ts(),
        events=list(events),  # type: ignore[arg-type]
        final_answer=final_answer,
    )


class TestHealthyTrajectory:
    def test_empty_failure_modes_when_run_looks_clean(self) -> None:
        traj = _trajectory(
            UserMessage(content="recommend a thriller"),
            ToolUse(tool_name="search_titles", arguments={"genres": ["Thriller"]}),
            ToolResult(
                tool_name="search_titles",
                result={"hits": [{"id": "m1"}], "total_matched": 1},
                duration_seconds=0.01,
            ),
            FinalAnswer(text="I'd recommend Memento — taut psychological thriller, 113 min."),
            final_answer="I'd recommend Memento — taut psychological thriller, 113 min.",
        )
        assert classify(traj) == []


class TestHallucinatedTool:
    @pytest.mark.parametrize(
        "error_message",
        [
            "no tool named 'bogus'; have ['search_titles']",
            "invalid arguments for 'search_titles': bad value",
            "agent 'explainer' is not permitted to call 'search_titles'",
        ],
    )
    def test_flagged_for_known_error_fragments(self, error_message: str) -> None:
        traj = _trajectory(
            ToolUse(tool_name="search_titles", arguments={}),
            ToolResult(tool_name="search_titles", error=error_message, duration_seconds=0.0),
            final_answer="ok",
        )
        # Just verify HALLUCINATED_TOOL is in the list — REFUSAL/OFF_TASK
        # may also fire because the answer is short.
        assert FailureMode.HALLUCINATED_TOOL in classify(traj)


class TestInfiniteLoop:
    def test_three_identical_consecutive_calls_flagged(self) -> None:
        same = ToolUse(tool_name="search_titles", arguments={"genres": ["Drama"]})
        traj = _trajectory(same, same, same, final_answer="something")
        assert FailureMode.INFINITE_LOOP in classify(traj)

    def test_two_identical_calls_not_flagged(self) -> None:
        same = ToolUse(tool_name="search_titles", arguments={"genres": ["Drama"]})
        traj = _trajectory(
            same,
            same,
            FinalAnswer(text="A reasonable recommendation grounded in catalog."),
            final_answer="A reasonable recommendation grounded in catalog.",
        )
        assert FailureMode.INFINITE_LOOP not in classify(traj)

    def test_different_arguments_not_flagged(self) -> None:
        # Three calls to search_titles with progressively different filters
        # — that's normal "tweak and retry" behavior, not a loop.
        traj = _trajectory(
            ToolUse(tool_name="search_titles", arguments={"min_year": 2010}),
            ToolUse(tool_name="search_titles", arguments={"min_year": 2015}),
            ToolUse(tool_name="search_titles", arguments={"min_year": 2018}),
            FinalAnswer(text="A reasonable recommendation grounded in catalog."),
            final_answer="A reasonable recommendation grounded in catalog.",
        )
        assert FailureMode.INFINITE_LOOP not in classify(traj)

    def test_dict_argument_order_does_not_break_detection(self) -> None:
        # Same logical call, different dict iteration orders shouldn't
        # mask the loop.
        traj = _trajectory(
            ToolUse(tool_name="search_titles", arguments={"a": 1, "b": 2}),
            ToolUse(tool_name="search_titles", arguments={"b": 2, "a": 1}),
            ToolUse(tool_name="search_titles", arguments={"a": 1, "b": 2}),
            final_answer="something",
        )
        assert FailureMode.INFINITE_LOOP in classify(traj)

    def test_nested_dict_order_does_not_break_detection(self) -> None:
        """Regression for gemini-code-assist review on PR #4: the loop
        signature must canonicalize *recursively*, not just at the
        top level. ``repr(sorted(items()))`` sorted top-level keys but
        left nested dicts ordered by insertion, so a nested filter
        with shuffled keys silently broke the comparison."""
        traj = _trajectory(
            ToolUse(
                tool_name="search_titles",
                arguments={"filter": {"genre": "Thriller", "year_min": 2015}},
            ),
            ToolUse(
                tool_name="search_titles",
                arguments={"filter": {"year_min": 2015, "genre": "Thriller"}},
            ),
            ToolUse(
                tool_name="search_titles",
                arguments={"filter": {"genre": "Thriller", "year_min": 2015}},
            ),
            final_answer="something",
        )
        assert FailureMode.INFINITE_LOOP in classify(traj)


class TestRefusalAndOffTask:
    def test_refusal_phrases_flagged(self) -> None:
        for phrase in (
            "I can't help with that, sorry.",
            "I'm not able to make recommendations without more context.",
            "I'm sorry, but I cannot recommend movies right now.",
        ):
            traj = _trajectory(final_answer=phrase)
            modes = classify(traj)
            assert FailureMode.REFUSAL in modes

    def test_short_final_answer_flagged_off_task(self) -> None:
        traj = _trajectory(final_answer="ok")
        assert FailureMode.OFF_TASK in classify(traj)

    def test_missing_final_answer_flagged_off_task(self) -> None:
        traj = _trajectory(
            UserMessage(content="recommend a thriller"),
            ToolUse(tool_name="search_titles", arguments={}),
        )
        assert FailureMode.OFF_TASK in classify(traj)

    def test_refusal_takes_precedence_over_off_task(self) -> None:
        # A short refusal answer satisfies BOTH heuristics; only one
        # should fire to avoid double-tagging.
        traj = _trajectory(final_answer="I can't.")
        modes = classify(traj)
        assert FailureMode.REFUSAL in modes
        assert FailureMode.OFF_TASK not in modes


class TestCostBlowup:
    def test_more_than_50_tool_calls_flagged(self) -> None:
        events = [ToolUse(tool_name="search_titles", arguments={"i": i}) for i in range(60)]
        traj = _trajectory(*events, final_answer="exhausted retries")
        assert FailureMode.COST_BLOWUP in classify(traj)

    def test_normal_tool_call_count_not_flagged(self) -> None:
        events = [ToolUse(tool_name="search_titles", arguments={"i": i}) for i in range(10)]
        traj = _trajectory(
            *events,
            final_answer="A reasonable recommendation grounded in catalog facts here.",
        )
        assert FailureMode.COST_BLOWUP not in classify(traj)


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        traj = _trajectory(
            ToolUse(tool_name="search_titles", arguments={}),
            ToolResult(tool_name="search_titles", error="no tool named 'x'", duration_seconds=0.0),
            final_answer="I can't help.",
        )
        assert classify(traj) == classify(traj) == classify(traj)


class TestAssistantTurnFallback:
    def test_refusal_in_assistant_turn_when_no_final_answer(self) -> None:
        # Some runs end without a FinalAnswer event but with a clear
        # refusal in the last AssistantTurn — we should still flag it.
        traj = _trajectory(
            UserMessage(content="recommend a movie"),
            AssistantTurn(text="I cannot recommend movies in this domain."),
        )
        modes = classify(traj)
        assert FailureMode.REFUSAL in modes

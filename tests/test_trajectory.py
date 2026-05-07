"""Unit tests for Trajectory and TrajectoryRecorder."""

from datetime import UTC, datetime

import pytest

from harness_weaver.trajectory import (
    AssistantTurn,
    FinalAnswer,
    ToolResult,
    ToolUse,
    Trajectory,
    TrajectoryRecorder,
    UserMessage,
)


class TestRecorder:
    def test_finish_returns_populated_trajectory(self) -> None:
        rec = TrajectoryRecorder(task_id="t1", configuration_name="c1")
        rec.user_message("hi")
        rec.assistant_turn("hello")
        rec.final_answer("done")
        traj = rec.finish()

        assert traj.task_id == "t1"
        assert traj.configuration_name == "c1"
        assert traj.event_types() == ["user_message", "assistant_turn", "final_answer"]
        assert traj.final_answer == "done"
        assert traj.completed_at >= traj.started_at

    def test_final_answer_none_if_no_final_event(self) -> None:
        rec = TrajectoryRecorder(task_id="t1", configuration_name="c1")
        rec.user_message("hi")
        traj = rec.finish()
        assert traj.final_answer is None

    def test_tool_use_and_result_recorded(self) -> None:
        rec = TrajectoryRecorder(task_id="t1", configuration_name="c1")
        rec.tool_use("search_titles", {"genres": ["Drama"]})
        rec.tool_result(
            "search_titles",
            result={"hits": [], "total_matched": 0},
            duration_seconds=0.01,
        )
        traj = rec.finish()
        assert traj.tool_call_count == 1
        assert isinstance(traj.events[0], ToolUse)
        assert traj.events[0].arguments == {"genres": ["Drama"]}
        assert isinstance(traj.events[1], ToolResult)
        assert traj.events[1].result == {"hits": [], "total_matched": 0}
        assert traj.events[1].error is None

    def test_tool_error_recorded(self) -> None:
        rec = TrajectoryRecorder(task_id="t1", configuration_name="c1")
        rec.tool_result(
            "get_metadata",
            error="no movie with id 'm999'",
            duration_seconds=0.0,
        )
        traj = rec.finish()
        assert isinstance(traj.events[0], ToolResult)
        assert traj.events[0].error == "no movie with id 'm999'"
        assert traj.events[0].result is None

    def test_agent_id_propagates(self) -> None:
        rec = TrajectoryRecorder(task_id="t1", configuration_name="c1")
        rec.assistant_turn("delegating", agent_id="orchestrator")
        rec.assistant_turn("searching", agent_id="discovery")
        traj = rec.finish()
        assert traj.events[0].agent_id == "orchestrator"
        assert traj.events[1].agent_id == "discovery"

    def test_duration_seconds_computed(self) -> None:
        rec = TrajectoryRecorder(task_id="t1", configuration_name="c1")
        traj = rec.finish()
        assert traj.duration_seconds >= 0.0


class TestTrajectorySerialization:
    def test_round_trip_through_json(self) -> None:
        original = Trajectory(
            task_id="t1",
            configuration_name="c1",
            started_at=datetime(2025, 1, 1, tzinfo=UTC),
            completed_at=datetime(2025, 1, 1, 0, 0, 1, tzinfo=UTC),
            events=[
                UserMessage(content="hi", timestamp=datetime(2025, 1, 1, tzinfo=UTC)),
                AssistantTurn(text="ok", timestamp=datetime(2025, 1, 1, tzinfo=UTC)),
                FinalAnswer(text="done", timestamp=datetime(2025, 1, 1, tzinfo=UTC)),
            ],
            final_answer="done",
        )
        serialized = original.model_dump_json()
        restored = Trajectory.model_validate_json(serialized)
        assert restored == original
        assert restored.event_types() == ["user_message", "assistant_turn", "final_answer"]

    def test_discriminated_union_rejects_unknown_type(self) -> None:
        bogus = (
            '{"task_id":"t","configuration_name":"c","started_at":"2025-01-01T00:00:00Z",'
            '"completed_at":"2025-01-01T00:00:00Z",'
            '"events":[{"type":"bogus","timestamp":"2025-01-01T00:00:00Z"}]}'
        )
        with pytest.raises(ValueError, match="union_tag_invalid"):
            Trajectory.model_validate_json(bogus)

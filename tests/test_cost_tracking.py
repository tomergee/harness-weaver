"""Cost and turn-count tracking on Trajectory.

Covers:
* Trajectory accepts ``total_cost_usd`` / ``num_turns`` and round-trips
  through JSON.
* TrajectoryRecorder.set_cost lands the values on ``finish()``.
* SdkMessageTranslator pulls them from ``ResultMessage``.
* Classifier's ``cost_blowup`` rule prefers real cost over the
  tool-call proxy when both could fire.
"""

from __future__ import annotations

from datetime import UTC, datetime

import claude_agent_sdk as sdk
import pytest

from harness_weaver.judge.classifier import FailureMode, classify
from harness_weaver.sdk_translate import SdkMessageTranslator
from harness_weaver.trajectory import ToolUse, Trajectory, TrajectoryRecorder


def _ts() -> datetime:
    return datetime(2025, 1, 1, tzinfo=UTC)


def _trajectory(
    *events: object,
    final_answer: str | None = None,
    total_cost_usd: float | None = None,
    num_turns: int | None = None,
) -> Trajectory:
    return Trajectory(
        task_id="t1",
        configuration_name="cfg",
        started_at=_ts(),
        completed_at=_ts(),
        events=list(events),  # type: ignore[arg-type]
        final_answer=final_answer,
        total_cost_usd=total_cost_usd,
        num_turns=num_turns,
    )


class TestTrajectoryFields:
    def test_defaults_to_none(self) -> None:
        traj = _trajectory(final_answer="ok")
        assert traj.total_cost_usd is None
        assert traj.num_turns is None

    def test_round_trip_through_json(self) -> None:
        traj = _trajectory(final_answer="ok", total_cost_usd=0.0123, num_turns=4)
        restored = Trajectory.model_validate_json(traj.model_dump_json())
        assert restored.total_cost_usd == 0.0123
        assert restored.num_turns == 4

    def test_negative_cost_rejected(self) -> None:
        with pytest.raises(ValueError):
            Trajectory(
                task_id="t",
                configuration_name="c",
                started_at=_ts(),
                completed_at=_ts(),
                events=[],
                total_cost_usd=-0.01,
            )

    def test_negative_num_turns_rejected(self) -> None:
        with pytest.raises(ValueError):
            Trajectory(
                task_id="t",
                configuration_name="c",
                started_at=_ts(),
                completed_at=_ts(),
                events=[],
                num_turns=-1,
            )


class TestRecorderSetCost:
    def test_set_cost_lands_on_finish(self) -> None:
        rec = TrajectoryRecorder(task_id="t1", configuration_name="cfg")
        rec.user_message("hi")
        rec.set_cost(total_cost_usd=0.05, num_turns=3)
        traj = rec.finish()
        assert traj.total_cost_usd == 0.05
        assert traj.num_turns == 3

    def test_default_when_set_cost_never_called(self) -> None:
        rec = TrajectoryRecorder(task_id="t1", configuration_name="cfg")
        rec.user_message("hi")
        traj = rec.finish()
        assert traj.total_cost_usd is None
        assert traj.num_turns is None

    def test_last_set_wins(self) -> None:
        # The SDK can emit multiple ResultMessages on retry; we keep the
        # most recent values rather than the first.
        rec = TrajectoryRecorder(task_id="t1", configuration_name="cfg")
        rec.set_cost(total_cost_usd=0.01, num_turns=1)
        rec.set_cost(total_cost_usd=0.02, num_turns=2)
        traj = rec.finish()
        assert traj.total_cost_usd == 0.02
        assert traj.num_turns == 2


class TestTranslatorPullsCost:
    def test_result_message_cost_lands_on_trajectory(self) -> None:
        rec = TrajectoryRecorder(task_id="t1", configuration_name="cfg")
        translator = SdkMessageTranslator()
        translator.translate(
            sdk.ResultMessage(
                subtype="success",
                duration_ms=500,
                duration_api_ms=400,
                is_error=False,
                num_turns=5,
                session_id="s",
                stop_reason="end_turn",
                total_cost_usd=0.0234,
                usage=None,
                result="done",
                structured_output=None,
                model_usage=None,
                permission_denials=None,
                deferred_tool_use=None,
                errors=None,
                api_error_status=None,
                uuid=None,
            ),
            rec,
        )
        traj = rec.finish()
        assert traj.total_cost_usd == 0.0234
        assert traj.num_turns == 5
        assert traj.final_answer == "done"

    def test_missing_cost_falls_through_as_none(self) -> None:
        # Older SDK versions may not surface total_cost_usd; the field is
        # Optional, and the recorder must accept None without crashing.
        rec = TrajectoryRecorder(task_id="t1", configuration_name="cfg")
        translator = SdkMessageTranslator()
        translator.translate(
            sdk.ResultMessage(
                subtype="success",
                duration_ms=500,
                duration_api_ms=400,
                is_error=False,
                num_turns=2,
                session_id="s",
                stop_reason="end_turn",
                total_cost_usd=None,
                usage=None,
                result="done",
                structured_output=None,
                model_usage=None,
                permission_denials=None,
                deferred_tool_use=None,
                errors=None,
                api_error_status=None,
                uuid=None,
            ),
            rec,
        )
        traj = rec.finish()
        assert traj.total_cost_usd is None
        assert traj.num_turns == 2


class TestCostBlowupRule:
    def test_real_cost_above_threshold_flags(self) -> None:
        # Real cost > $0.50 → cost_blowup, regardless of tool-call count.
        traj = _trajectory(
            ToolUse(tool_name="search_titles", arguments={}),
            final_answer="A reasonable recommendation grounded in the catalog.",
            total_cost_usd=0.75,
        )
        assert FailureMode.COST_BLOWUP in classify(traj)

    def test_real_cost_below_threshold_does_not_flag(self) -> None:
        # Real cost is the source of truth — even if tool calls would
        # have triggered the proxy, low real cost wins.
        events = [ToolUse(tool_name="search_titles", arguments={"i": i}) for i in range(60)]
        traj = _trajectory(
            *events,
            final_answer="Reasonable recommendation grounded in catalog.",
            total_cost_usd=0.05,
        )
        assert FailureMode.COST_BLOWUP not in classify(traj)

    def test_proxy_fires_when_cost_unavailable(self) -> None:
        # No real cost reported → fall back to the tool-call proxy.
        events = [ToolUse(tool_name="search_titles", arguments={"i": i}) for i in range(60)]
        traj = _trajectory(*events, final_answer="exhausted retries", total_cost_usd=None)
        assert FailureMode.COST_BLOWUP in classify(traj)

"""Unit tests for the structural-comparison report."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harness_weaver.judge.classifier import FailureMode
from harness_weaver.judge.structural import (
    StructuralReport,
    TrajectorySummary,
    render_markdown,
)
from harness_weaver.task import Task
from harness_weaver.trajectory import (
    FinalAnswer,
    ToolResult,
    ToolUse,
    Trajectory,
    UserMessage,
)


def _ts(seconds: int = 0) -> datetime:
    return datetime(2025, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds)


def _trajectory(
    *events: object,
    configuration_name: str = "cfg-a",
    final_answer: str = "A reasonable recommendation grounded in the catalog.",
    duration_seconds: int = 1,
) -> Trajectory:
    return Trajectory(
        task_id="t1",
        configuration_name=configuration_name,
        started_at=_ts(0),
        completed_at=_ts(duration_seconds),
        events=list(events),  # type: ignore[arg-type]
        final_answer=final_answer,
    )


class TestTrajectorySummary:
    def test_counts_match_trajectory(self) -> None:
        traj = _trajectory(
            UserMessage(content="x"),
            ToolUse(tool_name="search_titles", arguments={}),
            ToolResult(tool_name="search_titles", result={"hits": []}, duration_seconds=0.01),
            ToolUse(tool_name="get_metadata", arguments={"movie_id": "m1"}),
            ToolResult(
                tool_name="get_metadata", error="no movie with id 'm1'", duration_seconds=0.0
            ),
        )
        summary = TrajectorySummary.of(traj)
        assert summary.event_count == 5
        assert summary.tool_call_count == 2
        assert summary.tool_error_count == 1
        assert summary.has_final_answer is True
        assert summary.final_answer_chars > 0

    def test_failure_modes_propagate_from_classifier(self) -> None:
        traj = _trajectory(
            ToolUse(tool_name="search_titles", arguments={}),
            ToolResult(tool_name="search_titles", error="no tool named 'x'", duration_seconds=0.0),
            final_answer="I can't help.",
        )
        summary = TrajectorySummary.of(traj)
        assert FailureMode.HALLUCINATED_TOOL in summary.failure_modes
        assert FailureMode.REFUSAL in summary.failure_modes


class TestSuccessCriteria:
    @pytest.fixture
    def task_with_criteria(self) -> Task:
        return Task(
            task_id="t1",
            user_prompt="thrillers",
            success_criteria={
                "min_results": 1,
                "max_runtime_minutes": 120,
                "min_rating": 8.0,
                "must_include_genre": "Thriller",
            },
        )

    def _traj_with_search(
        self, hits: list[dict[str, object]], cfg_name: str = "cfg-a"
    ) -> Trajectory:
        return _trajectory(
            ToolUse(tool_name="search_titles", arguments={}),
            ToolResult(
                tool_name="search_titles",
                result={"hits": hits, "total_matched": len(hits)},
                duration_seconds=0.01,
            ),
            FinalAnswer(text="A reasonable recommendation grounded in the catalog."),
            configuration_name=cfg_name,
        )

    def test_all_pass_when_hits_satisfy_criteria(self, task_with_criteria: Task) -> None:
        traj = self._traj_with_search(
            [{"runtime_minutes": 110, "rating": 8.5, "genres": ["Thriller", "Drama"]}]
        )
        summary = TrajectorySummary.of(traj, task=task_with_criteria)
        assert summary.success_criteria == {
            "min_results": "pass",
            "max_runtime_minutes": "pass",
            "min_rating": "pass",
            "must_include_genre": "pass",
        }

    def test_failures_when_hits_violate_criteria(self, task_with_criteria: Task) -> None:
        # Runtime is too long, rating too low, wrong genre.
        traj = self._traj_with_search(
            [{"runtime_minutes": 150, "rating": 6.0, "genres": ["Comedy"]}]
        )
        summary = TrajectorySummary.of(traj, task=task_with_criteria)
        assert summary.success_criteria["max_runtime_minutes"] == "fail"
        assert summary.success_criteria["min_rating"] == "fail"
        assert summary.success_criteria["must_include_genre"] == "fail"

    def test_unknown_criterion_marked_unknown(self, task_with_criteria: Task) -> None:
        # Patch in a criterion the structural layer doesn't understand.
        custom_task = task_with_criteria.model_copy(
            update={"success_criteria": {"requires_user_satisfaction": True}}
        )
        traj = self._traj_with_search([{"runtime_minutes": 100, "rating": 8.0, "genres": []}])
        summary = TrajectorySummary.of(traj, task=custom_task)
        assert summary.success_criteria == {"requires_user_satisfaction": "unknown"}

    def test_no_search_hits_gives_unknown_for_hit_based_criteria(
        self, task_with_criteria: Task
    ) -> None:
        # If the agent never called search_titles, criteria that rely on
        # hit data can't be evaluated — must not silently pass-or-fail.
        traj = _trajectory(FinalAnswer(text="here is a recommendation grounded in nothing"))
        summary = TrajectorySummary.of(traj, task=task_with_criteria)
        # min_results expects 1, found 0 → fail (this *can* be evaluated).
        assert summary.success_criteria["min_results"] == "fail"
        # The bound checks have no hits to test → unknown.
        assert summary.success_criteria["max_runtime_minutes"] == "unknown"
        assert summary.success_criteria["min_rating"] == "unknown"
        assert summary.success_criteria["must_include_genre"] == "unknown"


class TestStructuralReport:
    def test_task_id_mismatch_rejected(self) -> None:
        a = _trajectory()
        b = Trajectory(
            task_id="different",
            configuration_name="cfg-b",
            started_at=_ts(0),
            completed_at=_ts(1),
            events=[],
            final_answer="ok",
        )
        with pytest.raises(ValueError, match="task_id mismatch"):
            StructuralReport.of(a, b)

    def test_round_trip(self) -> None:
        a = _trajectory(configuration_name="cfg-a")
        b = _trajectory(configuration_name="cfg-b")
        report = StructuralReport.of(a, b)
        assert report.task_id == "t1"
        assert report.a.configuration_name == "cfg-a"
        assert report.b.configuration_name == "cfg-b"


class TestMarkdownRender:
    def test_includes_both_configuration_names(self) -> None:
        a = _trajectory(configuration_name="single-agent-basic")
        b = _trajectory(configuration_name="single-agent-with-sandbox")
        md = render_markdown(StructuralReport.of(a, b))
        assert "single-agent-basic" in md
        assert "single-agent-with-sandbox" in md

    def test_failure_modes_section_says_none_when_clean(self) -> None:
        a = _trajectory()
        b = _trajectory(configuration_name="cfg-b")
        md = render_markdown(StructuralReport.of(a, b))
        assert "| Failure modes" in md
        assert "none" in md  # both sides clean

    def test_deterministic(self) -> None:
        # Same input → same output, line-for-line. Trajectories diff
        # cleanly across runs.
        a = _trajectory()
        b = _trajectory(configuration_name="cfg-b")
        md_1 = render_markdown(StructuralReport.of(a, b))
        md_2 = render_markdown(StructuralReport.of(a, b))
        assert md_1 == md_2

    def test_success_criteria_section_omitted_when_no_task(self) -> None:
        a = _trajectory()
        b = _trajectory(configuration_name="cfg-b")
        md = render_markdown(StructuralReport.of(a, b))
        assert "## Success criteria" not in md

    def test_success_criteria_section_present_when_task_provides_them(self) -> None:
        task = Task(
            task_id="t1",
            user_prompt="x",
            success_criteria={"min_results": 1},
        )
        a = _trajectory()
        b = _trajectory(configuration_name="cfg-b")
        md = render_markdown(StructuralReport.of(a, b, task=task))
        assert "## Success criteria" in md
        assert "min_results" in md

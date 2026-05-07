"""Pack-level aggregation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harness_weaver.judge.aggregate import (
    PackSummary,
    PerTaskSummary,
    render_pack_markdown,
)
from harness_weaver.judge.classifier import FailureMode
from harness_weaver.task import Task, TaskPack
from harness_weaver.trajectory import (
    ToolResult,
    ToolUse,
    Trajectory,
    UserMessage,
)


def _ts(seconds: int = 0) -> datetime:
    return datetime(2025, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds)


def _trajectory(
    *events: object,
    task_id: str,
    configuration_name: str = "cfg-a",
    final_answer: str = "A reasonable recommendation grounded in the catalog.",
    duration_seconds: int = 1,
    total_cost_usd: float | None = None,
) -> Trajectory:
    return Trajectory(
        task_id=task_id,
        configuration_name=configuration_name,
        started_at=_ts(0),
        completed_at=_ts(duration_seconds),
        events=list(events),  # type: ignore[arg-type]
        final_answer=final_answer,
        total_cost_usd=total_cost_usd,
    )


def _task(task_id: str, **kwargs: object) -> Task:
    return Task(task_id=task_id, user_prompt=f"prompt for {task_id}", **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def two_task_pack() -> TaskPack:
    return TaskPack(
        name="discovery",
        description="two prompts",
        tasks=[
            _task("t1", success_criteria={"min_results": 1, "max_runtime_minutes": 120}),
            _task("t2", success_criteria={"min_results": 1}),
        ],
    )


# --- Aggregate stats ----------------------------------------------------


class TestPackSummary:
    def test_completion_rate_with_one_unfinished(self, two_task_pack: TaskPack) -> None:
        a = _trajectory(task_id="t1", final_answer="ok-ish complete answer here")
        # b has no final_answer (None) — counts as not completed
        b = Trajectory(
            task_id="t2",
            configuration_name="cfg-a",
            started_at=_ts(0),
            completed_at=_ts(1),
            events=[UserMessage(content="x")],
            final_answer=None,
        )
        summary = PackSummary.of([a, b], pack=two_task_pack, configuration_name="cfg-a")
        assert summary.task_count == 2
        assert summary.completed_count == 1
        assert summary.completion_rate == 0.5

    def test_failure_mode_counts_tally_across_pack(self, two_task_pack: TaskPack) -> None:
        # t1: hallucinated tool. t2: refusal.
        a = _trajectory(
            ToolUse(tool_name="search_titles", arguments={}),
            ToolResult(tool_name="search_titles", error="no tool named 'x'", duration_seconds=0.0),
            task_id="t1",
            final_answer="ok",
        )
        b = _trajectory(task_id="t2", final_answer="I can't help with that.")
        summary = PackSummary.of([a, b], pack=two_task_pack, configuration_name="cfg-a")
        assert summary.failure_mode_counts[FailureMode.HALLUCINATED_TOOL] == 1
        assert summary.failure_mode_counts[FailureMode.REFUSAL] == 1

    def test_tool_call_stats(self, two_task_pack: TaskPack) -> None:
        a = _trajectory(
            ToolUse(tool_name="search_titles", arguments={"i": 0}),
            ToolUse(tool_name="search_titles", arguments={"i": 1}),
            ToolUse(tool_name="search_titles", arguments={"i": 2}),
            task_id="t1",
        )
        b = _trajectory(
            ToolUse(tool_name="search_titles", arguments={"i": 0}),
            task_id="t2",
        )
        summary = PackSummary.of([a, b], pack=two_task_pack, configuration_name="cfg-a")
        assert summary.total_tool_calls == 4
        assert summary.mean_tool_calls == 2.0
        assert summary.median_tool_calls == 2.0

    def test_total_cost_sums_when_tracked(self, two_task_pack: TaskPack) -> None:
        a = _trajectory(task_id="t1", total_cost_usd=0.0123)
        b = _trajectory(task_id="t2", total_cost_usd=0.0456)
        summary = PackSummary.of([a, b], pack=two_task_pack, configuration_name="cfg-a")
        assert summary.total_cost_usd == pytest.approx(0.0579)

    def test_total_cost_is_none_when_no_runs_tracked(self, two_task_pack: TaskPack) -> None:
        a = _trajectory(task_id="t1", total_cost_usd=None)
        b = _trajectory(task_id="t2", total_cost_usd=None)
        summary = PackSummary.of([a, b], pack=two_task_pack, configuration_name="cfg-a")
        assert summary.total_cost_usd is None

    def test_total_cost_is_partial_sum_when_some_tracked(self, two_task_pack: TaskPack) -> None:
        # One tracked, one not — surface only the tracked sum so the
        # number isn't misleadingly low.
        a = _trajectory(task_id="t1", total_cost_usd=0.05)
        b = _trajectory(task_id="t2", total_cost_usd=None)
        summary = PackSummary.of([a, b], pack=two_task_pack, configuration_name="cfg-a")
        assert summary.total_cost_usd == 0.05  # only the one tracked

    def test_per_task_in_input_order(self, two_task_pack: TaskPack) -> None:
        a = _trajectory(task_id="t1")
        b = _trajectory(task_id="t2")
        summary = PackSummary.of([a, b], pack=two_task_pack, configuration_name="cfg-a")
        assert [p.task_id for p in summary.per_task] == ["t1", "t2"]

    def test_empty_trajectories_list(self, two_task_pack: TaskPack) -> None:
        summary = PackSummary.of([], pack=two_task_pack, configuration_name="cfg-a")
        assert summary.task_count == 0
        assert summary.completion_rate == 0.0
        assert summary.mean_tool_calls == 0.0


# --- Success criteria aggregation ---------------------------------------


class TestSuccessCriteriaAggregation:
    def test_pass_rate_across_pack(self, two_task_pack: TaskPack) -> None:
        # Both tasks have min_results=1; only t1 has max_runtime.
        # t1 returns 1 hit at 100min (passes both); t2 returns 0 hits
        # (fails min_results, max_runtime not applicable).
        a = _trajectory(
            ToolUse(tool_name="search_titles", arguments={}),
            ToolResult(
                tool_name="search_titles",
                result={
                    "hits": [{"runtime_minutes": 100, "rating": 8.0, "genres": []}],
                    "total_matched": 1,
                },
                duration_seconds=0.01,
            ),
            task_id="t1",
        )
        b = _trajectory(
            ToolUse(tool_name="search_titles", arguments={}),
            ToolResult(
                tool_name="search_titles",
                result={"hits": [], "total_matched": 0},
                duration_seconds=0.01,
            ),
            task_id="t2",
        )
        summary = PackSummary.of([a, b], pack=two_task_pack, configuration_name="cfg-a")
        # min_results: t1 passes (1 ≥ 1), t2 fails (0 < 1) → 1/2
        assert summary.success_criteria["min_results"] == (1, 2)
        # max_runtime_minutes: only set on t1, where it passes → 1/1
        assert summary.success_criteria["max_runtime_minutes"] == (1, 1)


# --- Markdown rendering -------------------------------------------------


class TestMarkdownRender:
    def test_includes_pack_and_config_names(self, two_task_pack: TaskPack) -> None:
        a = _trajectory(task_id="t1")
        b = _trajectory(task_id="t2")
        md = render_pack_markdown(
            PackSummary.of([a, b], pack=two_task_pack, configuration_name="single-agent-basic")
        )
        assert "discovery" in md
        assert "single-agent-basic" in md

    def test_per_task_table_lists_every_task(self, two_task_pack: TaskPack) -> None:
        a = _trajectory(task_id="t1")
        b = _trajectory(task_id="t2")
        md = render_pack_markdown(
            PackSummary.of([a, b], pack=two_task_pack, configuration_name="cfg-a")
        )
        assert "`t1`" in md
        assert "`t2`" in md

    def test_failure_modes_section_omitted_when_clean(self, two_task_pack: TaskPack) -> None:
        a = _trajectory(task_id="t1")
        b = _trajectory(task_id="t2")
        md = render_pack_markdown(
            PackSummary.of([a, b], pack=two_task_pack, configuration_name="cfg-a")
        )
        assert "## Failure modes" not in md

    def test_failure_modes_section_present_when_any_fired(self, two_task_pack: TaskPack) -> None:
        a = _trajectory(task_id="t1", final_answer="I can't help.")  # refusal
        b = _trajectory(task_id="t2")
        md = render_pack_markdown(
            PackSummary.of([a, b], pack=two_task_pack, configuration_name="cfg-a")
        )
        assert "## Failure modes" in md
        assert "refusal" in md

    def test_deterministic(self, two_task_pack: TaskPack) -> None:
        # Same input → same output, line-for-line.
        a = _trajectory(task_id="t1", total_cost_usd=0.05)
        b = _trajectory(task_id="t2")
        summary = PackSummary.of([a, b], pack=two_task_pack, configuration_name="cfg-a")
        assert render_pack_markdown(summary) == render_pack_markdown(summary)

    def test_cost_dash_when_not_tracked(self, two_task_pack: TaskPack) -> None:
        a = _trajectory(task_id="t1")  # cost None
        b = _trajectory(task_id="t2")
        md = render_pack_markdown(
            PackSummary.of([a, b], pack=two_task_pack, configuration_name="cfg-a")
        )
        # The Total cost row should render '-' rather than '$0.0000'
        assert "Total cost (USD)       | -" in md


class TestPerTaskSummaryShape:
    def test_carries_failure_modes_and_cost(self) -> None:
        s = PerTaskSummary(
            task_id="t1",
            completed=True,
            failure_modes=[FailureMode.OFF_TASK],
            tool_call_count=3,
            duration_seconds=2.5,
            total_cost_usd=0.0123,
        )
        assert s.failure_modes == [FailureMode.OFF_TASK]
        assert s.total_cost_usd == 0.0123

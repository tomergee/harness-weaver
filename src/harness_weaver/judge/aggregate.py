"""Pack-level aggregation: roll up many trajectories into one report.

Where :mod:`.structural` compares two trajectories on the same task,
this module summarizes many trajectories from one configuration over
a whole :class:`~harness_weaver.task.TaskPack`. Output answers
"how did this configuration do across the pack?" in numbers a
reviewer can scan in 30 seconds.

Metrics covered:

* completion rate (fraction of tasks that produced a final answer)
* failure-mode tag frequencies across the pack
* per-criterion pass rate where ``Task.success_criteria`` was set
* tool-call count statistics (mean, median) and total duration
* total and mean cost when the SDK reported it

All deterministic, all pure-Python — no LLM, no API calls. Pairs
naturally with the per-task trajectories the eval command writes.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from harness_weaver.judge.classifier import FailureMode, classify
from harness_weaver.judge.structural import TrajectorySummary

if TYPE_CHECKING:
    from harness_weaver.task import Task, TaskPack
    from harness_weaver.trajectory import Trajectory


@dataclass(frozen=True)
class PerTaskSummary:
    """Single-task slice surfaced inside a :class:`PackSummary`."""

    task_id: str
    completed: bool
    failure_modes: list[FailureMode]
    tool_call_count: int
    duration_seconds: float
    total_cost_usd: float | None


@dataclass(frozen=True)
class PackSummary:
    """Aggregate report: one configuration, many tasks."""

    pack_name: str
    configuration_name: str
    task_count: int
    completed_count: int
    per_task: list[PerTaskSummary]
    failure_mode_counts: dict[FailureMode, int]
    # (passes, applicable) per criterion — "applicable" excludes runs where
    # the criterion came back ``"unknown"``. Pass rate is passes /
    # applicable when applicable > 0; otherwise the criterion is reported
    # as not evaluable across the pack.
    success_criteria: dict[str, tuple[int, int]] = field(default_factory=dict)
    total_tool_calls: int = 0
    mean_tool_calls: float = 0.0
    median_tool_calls: float = 0.0
    total_duration_seconds: float = 0.0
    total_cost_usd: float | None = None

    @classmethod
    def of(
        cls,
        trajectories: list[Trajectory],
        *,
        pack: TaskPack,
        configuration_name: str,
    ) -> PackSummary:
        """Build a :class:`PackSummary` from a config's trajectories over a pack.

        ``trajectories`` is expected to be in the same order as
        ``pack.tasks`` so per-task summaries align with the original task
        list. Tasks the harness skipped (rare) simply don't show up.
        """
        tasks_by_id = {t.task_id: t for t in pack.tasks}
        per_task = [_per_task_summary(t, tasks_by_id.get(t.task_id)) for t in trajectories]

        completed_count = sum(1 for s in per_task if s.completed)

        # Failure modes: count occurrences across the pack. Modes that
        # never fire are absent; reports show the empty case explicitly.
        failure_mode_counts: dict[FailureMode, int] = {}
        for s in per_task:
            for mode in s.failure_modes:
                failure_mode_counts[mode] = failure_mode_counts.get(mode, 0) + 1

        # Per-criterion pass rate. We rebuild from TrajectorySummary so
        # the same logic that drives the structural report drives the
        # rollup; no separate code path to keep in sync.
        success_criteria = _aggregate_success_criteria(trajectories, tasks_by_id)

        tool_calls = [s.tool_call_count for s in per_task]
        durations = [s.duration_seconds for s in per_task]
        costs = [s.total_cost_usd for s in per_task if s.total_cost_usd is not None]

        return cls(
            pack_name=pack.name,
            configuration_name=configuration_name,
            task_count=len(per_task),
            completed_count=completed_count,
            per_task=per_task,
            failure_mode_counts=failure_mode_counts,
            success_criteria=success_criteria,
            total_tool_calls=sum(tool_calls),
            mean_tool_calls=statistics.fmean(tool_calls) if tool_calls else 0.0,
            median_tool_calls=statistics.median(tool_calls) if tool_calls else 0.0,
            total_duration_seconds=sum(durations),
            total_cost_usd=sum(costs) if costs else None,
        )

    @property
    def completion_rate(self) -> float:
        return self.completed_count / self.task_count if self.task_count else 0.0


def render_pack_markdown(summary: PackSummary) -> str:
    """Render a :class:`PackSummary` as a deterministic markdown report.

    Sections (in fixed order so diffs across runs stay clean):

    1. Header with pack and configuration names.
    2. Aggregate stats table.
    3. Failure-mode frequencies (omitted when nothing fired).
    4. Success-criteria pass rates (omitted when none were set).
    5. Per-task table.
    """
    lines = [
        f"# Pack eval: {summary.pack_name}",
        "",
        f"Configuration: **{summary.configuration_name}**",
        "",
        "## Aggregate",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Tasks                  | {summary.task_count} |",
        (
            f"| Completed              | "
            f"{summary.completed_count} / {summary.task_count} "
            f"({summary.completion_rate:.0%}) |"
        ),
        f"| Total tool calls       | {summary.total_tool_calls} |",
        f"| Mean tool calls / task | {summary.mean_tool_calls:.1f} |",
        f"| Median tool calls      | {summary.median_tool_calls:.1f} |",
        f"| Total duration (s)     | {summary.total_duration_seconds:.2f} |",
        f"| Total cost (USD)       | {_money(summary.total_cost_usd)} |",
        "",
    ]

    if summary.failure_mode_counts:
        lines.append("## Failure modes")
        lines.append("")
        lines.append("| Mode | Count | % of tasks |")
        lines.append("| --- | --- | --- |")
        # Stable order: enum declaration order, only modes that fired.
        for mode in FailureMode:
            count = summary.failure_mode_counts.get(mode, 0)
            if count == 0:
                continue
            pct = count / summary.task_count if summary.task_count else 0.0
            lines.append(f"| `{mode.value}` | {count} | {pct:.0%} |")
        lines.append("")

    if summary.success_criteria:
        lines.append("## Success criteria")
        lines.append("")
        lines.append("| Criterion | Passes | Applicable | Pass rate |")
        lines.append("| --- | --- | --- | --- |")
        for key in sorted(summary.success_criteria):
            passes, applicable = summary.success_criteria[key]
            rate = f"{passes / applicable:.0%}" if applicable else "n/a"
            lines.append(f"| `{key}` | {passes} | {applicable} | {rate} |")
        lines.append("")

    lines.append("## Per-task")
    lines.append("")
    lines.append("| Task | Completed | Modes | Tool calls | Duration (s) | Cost (USD) |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for s in summary.per_task:
        modes = ", ".join(m.value for m in s.failure_modes) if s.failure_modes else "none"
        lines.append(
            f"| `{s.task_id}` | {_yn(s.completed)} | {modes} | "
            f"{s.tool_call_count} | {s.duration_seconds:.2f} | "
            f"{_money(s.total_cost_usd)} |"
        )
    lines.append("")
    return "\n".join(lines)


# --- internals ----------------------------------------------------------


def _per_task_summary(trajectory: Trajectory, task: Task | None) -> PerTaskSummary:
    return PerTaskSummary(
        task_id=trajectory.task_id,
        completed=trajectory.final_answer is not None,
        failure_modes=classify(trajectory, task=task),
        tool_call_count=trajectory.tool_call_count,
        duration_seconds=trajectory.duration_seconds,
        total_cost_usd=trajectory.total_cost_usd,
    )


def _aggregate_success_criteria(
    trajectories: list[Trajectory],
    tasks_by_id: dict[str, Task],
) -> dict[str, tuple[int, int]]:
    """Walk the per-task structural summaries and tally pass/applicable
    counts per criterion.

    "Applicable" excludes ``"unknown"`` results so criteria the
    structural layer couldn't evaluate don't drag the rate down.
    """
    counts: dict[str, list[int]] = {}  # key -> [passes, applicable]
    for trajectory in trajectories:
        task = tasks_by_id.get(trajectory.task_id)
        if task is None or not task.success_criteria:
            continue
        summary = TrajectorySummary.of(trajectory, task=task)
        for key, outcome in summary.success_criteria.items():
            entry = counts.setdefault(key, [0, 0])
            if outcome == "pass":
                entry[0] += 1
                entry[1] += 1
            elif outcome == "fail":
                entry[1] += 1
            # "unknown" — neither passes nor counts as applicable
    return {k: (v[0], v[1]) for k, v in counts.items()}


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def _money(value: float | None) -> str:
    if value is None:
        return "-"
    return f"${value:.4f}"


__all__ = [
    "PackSummary",
    "PerTaskSummary",
    "render_pack_markdown",
]

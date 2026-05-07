"""Structural comparison: rules-based diff of two trajectories.

Output is a :class:`StructuralReport` and a markdown rendering. No LLM
involved — this is the "what objectively happened" layer that runs
before, and complements, the LLM-as-judge quality verdict.

Metrics covered per side:

* event count, tool-call count, tool-error count
* wall-clock duration
* presence and length of a final answer
* failure-mode tags (from :mod:`.classifier`)
* success-criteria pass/fail when the Task carries them

Designed to be cheap to run, deterministic, and human-readable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from harness_weaver.judge.classifier import FailureMode, classify
from harness_weaver.trajectory import ToolResult

if TYPE_CHECKING:
    from harness_weaver.task import Task
    from harness_weaver.trajectory import Trajectory


@dataclass(frozen=True)
class TrajectorySummary:
    """One side of a comparison: structural facts about a single trajectory."""

    configuration_name: str
    event_count: int
    tool_call_count: int
    tool_error_count: int
    duration_seconds: float
    total_cost_usd: float | None
    num_turns: int | None
    has_final_answer: bool
    final_answer_chars: int
    failure_modes: list[FailureMode]
    # Result of evaluating each ``Task.success_criteria`` key. Empty when
    # the task didn't ship any criteria or none could be evaluated. Keys
    # mirror the original criteria; values are ``"pass"``, ``"fail"``,
    # or ``"unknown"`` (criteria the structural layer can't check —
    # e.g. ``must_include_genre`` requires reading the agent's text).
    success_criteria: dict[str, str] = field(default_factory=dict)

    @classmethod
    def of(cls, trajectory: Trajectory, *, task: Task | None = None) -> TrajectorySummary:
        tool_errors = sum(
            1 for ev in trajectory.events if isinstance(ev, ToolResult) and ev.error is not None
        )
        return cls(
            configuration_name=trajectory.configuration_name,
            event_count=len(trajectory.events),
            tool_call_count=trajectory.tool_call_count,
            tool_error_count=tool_errors,
            duration_seconds=trajectory.duration_seconds,
            total_cost_usd=trajectory.total_cost_usd,
            num_turns=trajectory.num_turns,
            has_final_answer=trajectory.final_answer is not None,
            final_answer_chars=len(trajectory.final_answer or ""),
            failure_modes=classify(trajectory, task=task),
            success_criteria=_evaluate_criteria(trajectory, task),
        )


@dataclass(frozen=True)
class StructuralReport:
    """Side-by-side structural comparison of two trajectories on the same task."""

    task_id: str
    a: TrajectorySummary
    b: TrajectorySummary

    @classmethod
    def of(
        cls,
        trajectory_a: Trajectory,
        trajectory_b: Trajectory,
        *,
        task: Task | None = None,
    ) -> StructuralReport:
        if trajectory_a.task_id != trajectory_b.task_id:
            raise ValueError(
                f"task_id mismatch: {trajectory_a.task_id!r} vs {trajectory_b.task_id!r}; "
                f"structural comparison only makes sense on a single task"
            )
        return cls(
            task_id=trajectory_a.task_id,
            a=TrajectorySummary.of(trajectory_a, task=task),
            b=TrajectorySummary.of(trajectory_b, task=task),
        )


def render_markdown(report: StructuralReport) -> str:
    """Render the structural report as a side-by-side markdown table.

    Output is stable: same input → same output, line-for-line. That
    makes the report committable and diffable across runs.
    """
    a, b = report.a, report.b
    lines = [
        f"# Structural comparison: {report.task_id}",
        "",
        f"|                       | {a.configuration_name} | {b.configuration_name} |",
        "| --- | --- | --- |",
        f"| Event count           | {a.event_count} | {b.event_count} |",
        f"| Tool calls            | {a.tool_call_count} | {b.tool_call_count} |",
        f"| Tool errors           | {a.tool_error_count} | {b.tool_error_count} |",
        f"| Duration (seconds)    | {a.duration_seconds:.2f} | {b.duration_seconds:.2f} |",
        f"| Cost (USD)            | {_money(a.total_cost_usd)} | {_money(b.total_cost_usd)} |",
        f"| Turns                 | {_or_dash(a.num_turns)} | {_or_dash(b.num_turns)} |",
        f"| Final answer present  | {_yn(a.has_final_answer)} | {_yn(b.has_final_answer)} |",
        f"| Final answer chars    | {a.final_answer_chars} | {b.final_answer_chars} |",
        f"| Failure modes         | {_modes(a.failure_modes)} | {_modes(b.failure_modes)} |",
        "",
    ]
    if a.success_criteria or b.success_criteria:
        keys = sorted(set(a.success_criteria) | set(b.success_criteria))
        lines.append("## Success criteria")
        lines.append("")
        lines.append(f"| Criterion | {a.configuration_name} | {b.configuration_name} |")
        lines.append("| --- | --- | --- |")
        for key in keys:
            lines.append(
                f"| `{key}` | "
                f"{a.success_criteria.get(key, 'unknown')} | "
                f"{b.success_criteria.get(key, 'unknown')} |"
            )
        lines.append("")
    return "\n".join(lines)


# --- internals ----------------------------------------------------------


def _yn(value: bool) -> str:
    return "yes" if value else "no"


def _money(value: float | None) -> str:
    """Render a USD cost or ``-`` when not tracked.

    Six decimal places — see the matching ``_money`` in
    :mod:`.aggregate` for the precision rationale. Kept in sync with
    that copy; refactor into a shared helper if a third callsite shows up.
    """
    if value is None:
        return "-"
    return f"${value:.6f}"


def _or_dash(value: int | None) -> str:
    return "-" if value is None else str(value)


def _modes(modes: list[FailureMode]) -> str:
    if not modes:
        return "none"
    return ", ".join(m.value for m in modes)


def _evaluate_criteria(trajectory: Trajectory, task: Task | None) -> dict[str, str]:
    """Apply each ``task.success_criteria`` entry to the trajectory.

    Recognised keys:
        * ``min_results`` (int): at least one tool call returned this many
          hits in its ``hits`` list.
        * ``max_runtime_minutes`` (int): every hit returned by any
          ``search_titles`` call has ``runtime_minutes <= value``.
        * ``min_runtime_minutes`` (int): every hit has runtime ≥ value.
        * ``min_rating`` (float): every hit has rating ≥ value.
        * ``must_include_genre`` (str): every hit lists this genre.

    Other keys land as ``"unknown"`` so callers see what the structural
    layer didn't evaluate (vs. silently dropping criteria). The LLM
    judge can still reason about them.
    """
    if task is None or not task.success_criteria:
        return {}

    hits = _all_search_hits(trajectory)
    out: dict[str, str] = {}
    for key, expected in task.success_criteria.items():
        out[key] = _check_criterion(key, expected, hits)
    return out


def _check_criterion(key: str, expected: object, hits: list[dict[str, Any]]) -> str:
    if key == "min_results":
        if not isinstance(expected, int):
            return "unknown"
        return "pass" if len(hits) >= expected else "fail"
    if key == "max_results":
        if not isinstance(expected, int):
            return "unknown"
        return "pass" if len(hits) <= expected else "fail"
    if key == "max_runtime_minutes":
        if not isinstance(expected, int) or not hits:
            return "unknown"
        return _all(h.get("runtime_minutes", 0) <= expected for h in hits)
    if key == "min_runtime_minutes":
        if not isinstance(expected, int) or not hits:
            return "unknown"
        return _all(h.get("runtime_minutes", 0) >= expected for h in hits)
    if key == "min_rating":
        if not isinstance(expected, int | float) or not hits:
            return "unknown"
        return _all(h.get("rating", 0) >= expected for h in hits)
    if key == "must_include_genre":
        if not isinstance(expected, str) or not hits:
            return "unknown"
        wanted = expected.lower()
        return _all(any(g.lower() == wanted for g in h.get("genres", [])) for h in hits)
    return "unknown"


def _all(check: Any) -> str:
    return "pass" if all(check) else "fail"


def _all_search_hits(trajectory: Trajectory) -> list[dict[str, Any]]:
    """Pull every ``hit`` from any tool result that carries a ``hits`` list.

    ``hits`` is the convention for "list of catalog matches" —
    :class:`SearchTitlesTool` ships it; new retrieval-shaped tools
    that want their results fed into criteria checks should follow
    the same shape (e.g. a ``find_titles`` tool returning
    ``{"hits": [...], "total_matched": N}``).

    We deliberately do *not* gate on ``tool_name == "search_titles"``
    so the structural layer doesn't have to be edited every time a
    new search-shaped tool is added. If finer control is needed, a
    future ``Task.success_criteria`` could carry an explicit
    ``hits_from`` list of tool names; we'll add that when a real use
    case demands it.
    """
    hits: list[dict[str, Any]] = []
    for event in trajectory.events:
        if not isinstance(event, ToolResult) or event.result is None:
            continue
        result_hits = event.result.get("hits")
        if isinstance(result_hits, list):
            hits.extend(h for h in result_hits if isinstance(h, dict))
    return hits


__all__ = [
    "StructuralReport",
    "TrajectorySummary",
    "render_markdown",
]

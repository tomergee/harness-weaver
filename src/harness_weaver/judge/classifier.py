"""Failure-mode classification: rules-based tags over a Trajectory.

The README's design notes commit the harness to auto-classifying failure
modes (``hallucinated_tool``, ``infinite_loop``, ``off_task``, etc.) so
post-hoc analysis can ask "which configurations failed *how* often?"
without re-reading every trajectory by hand.

The rules here are deliberately conservative — false positives are worse
than false negatives, because a flagged trajectory is a finger-pointing
artifact. If the rules are unsure, they don't tag.

This module never calls an LLM. The :mod:`harness_weaver.judge.llm`
module does the quality verdict; this one does the structural
diagnostics that don't need a model.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING

from harness_weaver.trajectory import (
    AssistantTurn,
    FinalAnswer,
    ToolResult,
    ToolUse,
)

if TYPE_CHECKING:
    from harness_weaver.task import Task
    from harness_weaver.trajectory import Trajectory


class FailureMode(StrEnum):
    """Tags applied to a trajectory's failure analysis. Empty list = no
    detected failure mode.

    Mirrors the set listed in the project README; values are stable
    strings so they round-trip through JSON in reports.
    """

    HALLUCINATED_TOOL = "hallucinated_tool"
    """The agent invoked a tool name that isn't registered, or passed
    arguments the schema rejected. Detected via ToolResult.error
    matching the registry's ``no tool named`` or the tool layer's
    ``invalid arguments`` messages."""

    INFINITE_LOOP = "infinite_loop"
    """The agent called the same tool with identical arguments three or
    more times consecutively without successful intervening progress.
    Conservative threshold: a normal "search again with tweaked filters"
    pattern looks like *different* arguments and isn't flagged."""

    OFF_TASK = "off_task"
    """The trajectory ended without a final answer, or with a final
    answer so short it can't address the user's prompt. The latter
    catches refusals that got through the explicit refusal heuristic."""

    REFUSAL = "refusal"
    """The final answer matches refusal patterns ("I can't", "I'm not
    able to") rather than attempting the task. Distinct from
    OFF_TASK so callers can differentiate a model that declined from
    one that just stalled."""

    COST_BLOWUP = "cost_blowup"
    """The trajectory exceeded a configured cost or turn cap. We don't
    record cost in the trajectory yet, so this fires only on extreme
    tool-call counts (>50) as a proxy."""

    OTHER = "other"
    """Catch-all for trajectories that look broken in ways we can't
    pinpoint. Reserved for callers wiring their own heuristics; the
    built-in classifier never emits this."""


# Tunable thresholds. Surfaced as module constants so tests and reports
# can reference them rather than hard-coding numbers in two places.
_INFINITE_LOOP_REPEATS = 3
_COST_BLOWUP_TOOL_CALLS = 50
_OFF_TASK_MIN_ANSWER_CHARS = 30

_REFUSAL_MARKERS: tuple[str, ...] = (
    "i can't",
    "i cannot",
    "i'm unable",
    "i am unable",
    "i'm not able",
    "i am not able",
    "i won't",
    "i refuse",
    "i'm sorry, but i can",
    "i apologize, but i can",
)

_HALLUCINATION_ERROR_FRAGMENTS: tuple[str, ...] = (
    "no tool named",
    "invalid arguments",
    "not permitted",  # also flagged under HALLUCINATED_TOOL — the agent
    # tried something it shouldn't have known about
)


def classify(trajectory: Trajectory, *, task: Task | None = None) -> list[FailureMode]:
    """Classify the failure modes a trajectory exhibits.

    Returns an empty list when the trajectory looks healthy. Order
    of returned modes is deterministic (enum declaration order) so
    reports diff cleanly between runs.

    ``task`` is optional and currently unused but accepted so callers
    can pass it without restructuring; future heuristics that compare
    the final answer to ``task.expected_outcome`` will lean on it.
    """
    del task  # reserved for future use; keep the signature stable
    detected: list[FailureMode] = []
    if _has_hallucinated_tool(trajectory):
        detected.append(FailureMode.HALLUCINATED_TOOL)
    if _has_infinite_loop(trajectory):
        detected.append(FailureMode.INFINITE_LOOP)
    if _is_refusal(trajectory):
        detected.append(FailureMode.REFUSAL)
    elif _is_off_task(trajectory):
        # Refusal is a *kind* of off-task; emit only one of the two so
        # reports don't double-count.
        detected.append(FailureMode.OFF_TASK)
    if _is_cost_blowup(trajectory):
        detected.append(FailureMode.COST_BLOWUP)
    return detected


# --- individual rules ---------------------------------------------------


def _has_hallucinated_tool(trajectory: Trajectory) -> bool:
    for event in trajectory.events:
        if isinstance(event, ToolResult) and event.error is not None:
            err_lower = event.error.lower()
            if any(frag in err_lower for frag in _HALLUCINATION_ERROR_FRAGMENTS):
                return True
    return False


def _has_infinite_loop(trajectory: Trajectory) -> bool:
    """Three consecutive ToolUse events with identical (name, arguments)."""
    streak = 0
    last_signature: tuple[str, str] | None = None
    for event in trajectory.events:
        if not isinstance(event, ToolUse):
            continue
        signature = (event.tool_name, _canonical_args(event.arguments))
        if signature == last_signature:
            streak += 1
            if streak >= _INFINITE_LOOP_REPEATS:
                return True
        else:
            streak = 1
            last_signature = signature
    return False


def _canonical_args(arguments: dict[str, object]) -> str:
    """Stable string form of tool arguments for equality comparison.

    ``json.dumps(..., sort_keys=True)`` recursively sorts nested dicts,
    so two calls that differ only in key insertion order — at any
    depth — produce the same canonical form. ``default=str`` is a
    defensive fallback for the rare case where a value isn't natively
    JSON-serializable; we don't expect this in practice (the SDK
    arrives at us as JSON in the first place) but it keeps loop
    detection from blowing up on unexpected types.
    """
    return json.dumps(arguments, sort_keys=True, default=str)


def _is_refusal(trajectory: Trajectory) -> bool:
    final = _final_text(trajectory)
    if final is None:
        return False
    head = final[:200].lower().strip()
    return any(marker in head for marker in _REFUSAL_MARKERS)


def _is_off_task(trajectory: Trajectory) -> bool:
    if trajectory.final_answer is None:
        return True
    return len(trajectory.final_answer.strip()) < _OFF_TASK_MIN_ANSWER_CHARS


def _is_cost_blowup(trajectory: Trajectory) -> bool:
    return trajectory.tool_call_count > _COST_BLOWUP_TOOL_CALLS


def _final_text(trajectory: Trajectory) -> str | None:
    """Best-effort final assistant text: ``final_answer`` if set, else the
    last AssistantTurn's text, else None."""
    if trajectory.final_answer:
        return trajectory.final_answer
    for event in reversed(trajectory.events):
        if isinstance(event, FinalAnswer | AssistantTurn) and event.text.strip():
            return event.text
    return None


__all__ = [
    "FailureMode",
    "classify",
]

"""LLM-as-judge: rank two trajectories on the same task using a model.

Wraps :mod:`inspect_ai.model` so the judge inherits inspect-ai's
provider abstraction (Anthropic today; swappable tomorrow), retry
behavior, and budget hooks. Our :class:`Judge` protocol is intentionally
narrow: one method, async, returns a :class:`JudgeVerdict`.

Two implementations:

* :class:`InspectAILlmJudge` — production. Asks Claude to compare the
  trajectories on a fixed rubric and emit JSON.
* :class:`FixedJudge` — test fake; returns a canned verdict regardless
  of input. Useful when wiring the CLI without an API key.

The structural layer (:mod:`.structural`) feeds into the prompt as a
pre-computed scaffold so the model doesn't have to count tool calls
itself; it gets the numbers and reasons about them.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from harness_weaver.judge.structural import StructuralReport, render_markdown

if TYPE_CHECKING:
    from harness_weaver.task import Task
    from harness_weaver.trajectory import Trajectory

DEFAULT_JUDGE_MODEL = "anthropic/claude-haiku-4-5-20251001"
"""Cheapest credible Anthropic model. Override at runtime if you want
a stronger judge — the verdict shape is model-agnostic."""


class JudgeVerdict(BaseModel):
    """Structured output of the LLM judge.

    ``winner`` carries one of four discrete outcomes; ``confidence`` is
    a self-reported 0-1 score (we don't try to calibrate it across
    runs — that's what the README's calibration set is for, future
    work). ``reasoning`` is free-form prose explaining the verdict.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    winner: Literal["a", "b", "tie", "both_fail"]
    reasoning: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    structural: StructuralReport | None = Field(
        default=None,
        description=(
            "The structural report fed to the judge, attached for "
            "traceability. None if the judge was called outside the "
            "normal compare flow."
        ),
    )


class Judge(ABC):
    """Strategy for producing a quality verdict on a (a, b) trajectory pair."""

    @abstractmethod
    async def verdict(
        self,
        *,
        task: Task,
        trajectory_a: Trajectory,
        trajectory_b: Trajectory,
    ) -> JudgeVerdict:
        """Compare the two trajectories. Returns a populated verdict."""


# --- inspect-ai backed implementation -----------------------------------


_SYSTEM_PROMPT = """You are an expert evaluator comparing two AI agents that ran on the same task. \
Your job is to decide which agent did better, or whether they tied, or whether both failed.

Evaluate on this rubric (in priority order):

1. **Task fidelity.** Did the agent address what the user actually asked for? An eloquent answer to the wrong question loses to a plain answer to the right one.
2. **Grounding.** Are the claims in the final answer supported by tool results visible in the trajectory? Hallucinated titles, ratings, or runtimes are disqualifying.
3. **Tool use efficiency.** Did the agent reach its answer with reasonable tool calls, or thrash? Repeated identical searches and tool errors count against it.
4. **Final answer quality.** Clarity, justification grounded in catalog facts, fit to the user's stated mood/constraints.

Return your verdict as JSON matching this schema:

  {
    "winner": "a" | "b" | "tie" | "both_fail",
    "reasoning": <prose explaining the verdict, citing specific events>,
    "confidence": <float between 0 and 1>
  }

Only return the JSON. No commentary before or after."""


class InspectAILlmJudge(Judge):
    """Production judge: uses :func:`inspect_ai.model.get_model` for LLM access.

    Args:
        model: Inspect-AI model id. Defaults to Haiku for cost; pass
            ``"anthropic/claude-sonnet-4-6"`` (or similar) for a
            stronger judge if Haiku's calls feel under-confident.

    The judge is async — wraps inspect-ai's async ``Model.generate``.
    Sync callers can drive it via ``asyncio.run`` (the CLI does this).
    """

    def __init__(self, *, model: str = DEFAULT_JUDGE_MODEL) -> None:
        # Lazy import so the heavy inspect-ai dependency only loads
        # when the LLM judge is actually constructed.
        from inspect_ai.model import get_model

        self._model = get_model(model)

    async def verdict(
        self,
        *,
        task: Task,
        trajectory_a: Trajectory,
        trajectory_b: Trajectory,
    ) -> JudgeVerdict:
        from inspect_ai.model import ChatMessageSystem, ChatMessageUser

        report = StructuralReport.of(trajectory_a, trajectory_b, task=task)
        prompt = _build_user_prompt(task, report, trajectory_a, trajectory_b)

        result = await self._model.generate(
            input=[
                ChatMessageSystem(content=_SYSTEM_PROMPT),
                ChatMessageUser(content=prompt),
            ]
        )
        verdict = _parse_verdict(result.completion)
        # Attach the structural report so report consumers see exactly
        # what numbers the LLM was reasoning over.
        return verdict.model_copy(update={"structural": report})


# --- a useful fake for tests / API-key-less smoke tests -----------------


class FixedJudge(Judge):
    """Returns a fixed verdict regardless of input. Useful for wiring tests."""

    def __init__(
        self,
        *,
        winner: Literal["a", "b", "tie", "both_fail"] = "tie",
        reasoning: str = "fixed verdict for testing",
        confidence: float = 0.5,
    ) -> None:
        self._verdict = JudgeVerdict(winner=winner, reasoning=reasoning, confidence=confidence)

    async def verdict(
        self,
        *,
        task: Task,
        trajectory_a: Trajectory,
        trajectory_b: Trajectory,
    ) -> JudgeVerdict:
        report = StructuralReport.of(trajectory_a, trajectory_b, task=task)
        return self._verdict.model_copy(update={"structural": report})


# --- prompt + parsing helpers -------------------------------------------


def _build_user_prompt(
    task: Task,
    report: StructuralReport,
    trajectory_a: Trajectory,
    trajectory_b: Trajectory,
) -> str:
    return (
        f"# Task\n\n"
        f"**ID:** {task.task_id}\n\n"
        f"**User prompt:** {task.user_prompt}\n\n"
        f"**Expected outcome:** {task.expected_outcome or '(not provided)'}\n\n"
        f"# Structural facts (computed deterministically; trust these counts)\n\n"
        f"{render_markdown(report)}\n\n"
        f"# Trajectory A: {trajectory_a.configuration_name}\n\n"
        f"{_render_trajectory_for_judge(trajectory_a)}\n\n"
        f"# Trajectory B: {trajectory_b.configuration_name}\n\n"
        f"{_render_trajectory_for_judge(trajectory_b)}\n\n"
        f"# Verdict\n\n"
        f"Apply the rubric and emit the JSON. No commentary."
    )


def _render_trajectory_for_judge(trajectory: Trajectory, *, max_result_chars: int = 600) -> str:
    """Render a trajectory as a compact event log the judge can read.

    Tool results are truncated to ``max_result_chars`` so the judge
    isn't drowning in 50-row search hits — what matters for the
    verdict is the *shape* of the call sequence and the final answer,
    not every byte the catalog returned.
    """
    from harness_weaver.trajectory import (
        AssistantTurn,
        FinalAnswer,
        ToolResult,
        ToolUse,
        UserMessage,
    )

    lines: list[str] = []
    for event in trajectory.events:
        prefix = f"[{event.agent_id}]"
        if isinstance(event, UserMessage):
            lines.append(f"{prefix} USER: {event.content}")
        elif isinstance(event, AssistantTurn):
            lines.append(f"{prefix} ASSISTANT: {event.text}")
        elif isinstance(event, ToolUse):
            args = json.dumps(event.arguments, ensure_ascii=False)
            lines.append(f"{prefix} TOOL_USE {event.tool_name}: {args}")
        elif isinstance(event, ToolResult):
            if event.error is not None:
                lines.append(f"{prefix} TOOL_RESULT {event.tool_name}: ERROR {event.error}")
            else:
                payload = json.dumps(event.result, ensure_ascii=False)
                if len(payload) > max_result_chars:
                    payload = payload[:max_result_chars] + f"... ({len(payload)} chars)"
                lines.append(f"{prefix} TOOL_RESULT {event.tool_name}: {payload}")
        elif isinstance(event, FinalAnswer):
            lines.append(f"{prefix} FINAL_ANSWER: {event.text}")
    return "\n".join(lines)


def _parse_verdict(text: str) -> JudgeVerdict:
    r"""Parse the model's JSON reply into a :class:`JudgeVerdict`.

    The model is instructed to emit only JSON, but defensively strips a
    common failure mode: a leading ```json fence. If parsing fails
    entirely, raises a clear error with the offending text so the
    caller can debug the model's output.
    """
    raw = text.strip()
    if raw.startswith("```"):
        # Strip a Markdown fence; the model occasionally adds one despite
        # being told not to.
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[len("json") :]
        raw = raw.strip("`").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"judge model emitted non-JSON output (first 400 chars): {text[:400]!r}"
        ) from exc
    return JudgeVerdict.model_validate(data)


__all__ = [
    "DEFAULT_JUDGE_MODEL",
    "FixedJudge",
    "InspectAILlmJudge",
    "Judge",
    "JudgeVerdict",
]

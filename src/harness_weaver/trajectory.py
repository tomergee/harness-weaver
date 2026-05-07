"""Trajectory: the auditable record of one agent run.

Five event kinds form a discriminated union:

    UserMessage    — the prompt the harness handed to the agent.
    AssistantTurn  — assistant text emitted between tool calls.
    ToolUse        — the agent decided to call a tool with these arguments.
    ToolResult     — what the tool returned (or the error it raised).
    FinalAnswer    — the agent's terminal response.

Every event carries an ``agent_id`` so multi-agent runs are attributable
(orchestrator vs. worker). For single-agent runs ``agent_id`` is always
``"orchestrator"``.

Trajectories are pydantic models so they round-trip through JSON without
custom serializers, which is what the eval pipeline (and ``examples/output/``
fixtures) need.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field


def _now() -> datetime:
    return datetime.now(UTC)


class _EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: datetime = Field(default_factory=_now)
    agent_id: str = Field(default="orchestrator")


class UserMessage(_EventBase):
    type: Literal["user_message"] = "user_message"
    content: str


class AssistantTurn(_EventBase):
    type: Literal["assistant_turn"] = "assistant_turn"
    text: str


class ToolUse(_EventBase):
    type: Literal["tool_use"] = "tool_use"
    tool_name: str
    arguments: dict[str, Any]


class ToolResult(_EventBase):
    type: Literal["tool_result"] = "tool_result"
    tool_name: str
    result: dict[str, Any] | None = Field(
        default=None,
        description="Tool output as a JSON-serializable dict; None when the tool errored.",
    )
    error: str | None = Field(
        default=None,
        description="Error message if the tool raised; None on success.",
    )
    duration_seconds: float = Field(ge=0.0)


class FinalAnswer(_EventBase):
    type: Literal["final_answer"] = "final_answer"
    text: str


TrajectoryEvent = Annotated[
    UserMessage | AssistantTurn | ToolUse | ToolResult | FinalAnswer,
    Field(discriminator="type"),
]


class Trajectory(BaseModel):
    """Frozen record of one (task, configuration) run.

    Use :class:`TrajectoryRecorder` to build one event-by-event during a run.
    The judge consumes ``Trajectory`` instances directly.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    configuration_name: str
    started_at: datetime
    completed_at: datetime
    events: list[TrajectoryEvent]
    final_answer: str | None = Field(
        default=None,
        description="The last FinalAnswer event's text; None if the run did not terminate cleanly.",
    )
    total_cost_usd: float | None = Field(
        default=None,
        ge=0.0,
        description=(
            "Total provider cost for the run, in USD, when the SDK reported one. "
            "None for fake/scripted runs and for SDK paths that don't surface "
            "cost (e.g. older inspect-ai versions). Trajectories from different "
            "providers may have different cost-accounting semantics — treat the "
            "value as advisory, not a billing record."
        ),
    )
    num_turns: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Number of model turns the SDK reported for this run. None when not available."
        ),
    )

    @property
    def tool_calls(self) -> list[ToolUse]:
        return [e for e in self.events if isinstance(e, ToolUse)]

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)

    @property
    def duration_seconds(self) -> float:
        return (self.completed_at - self.started_at).total_seconds()

    def event_types(self) -> list[str]:
        return [e.type for e in self.events]

    @classmethod
    def model_validate_path(cls, path: Any) -> Self:
        from pathlib import Path

        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


class TrajectoryRecorder:
    """Mutable builder. Construct one per run; freeze with :meth:`finish`."""

    def __init__(self, *, task_id: str, configuration_name: str) -> None:
        self._task_id = task_id
        self._configuration_name = configuration_name
        self._started_at = _now()
        self._events: list[TrajectoryEvent] = []
        self._final_answer: str | None = None
        self._total_cost_usd: float | None = None
        self._num_turns: int | None = None

    def record(self, event: TrajectoryEvent) -> None:
        self._events.append(event)
        if isinstance(event, FinalAnswer):
            self._final_answer = event.text

    def user_message(self, content: str, *, agent_id: str = "orchestrator") -> None:
        self.record(UserMessage(content=content, agent_id=agent_id))

    def assistant_turn(self, text: str, *, agent_id: str = "orchestrator") -> None:
        self.record(AssistantTurn(text=text, agent_id=agent_id))

    def tool_use(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        agent_id: str = "orchestrator",
    ) -> None:
        self.record(ToolUse(tool_name=tool_name, arguments=arguments, agent_id=agent_id))

    def tool_result(
        self,
        tool_name: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        duration_seconds: float,
        agent_id: str = "orchestrator",
    ) -> None:
        self.record(
            ToolResult(
                tool_name=tool_name,
                result=result,
                error=error,
                duration_seconds=duration_seconds,
                agent_id=agent_id,
            )
        )

    def final_answer(self, text: str, *, agent_id: str = "orchestrator") -> None:
        self.record(FinalAnswer(text=text, agent_id=agent_id))

    def set_cost(self, *, total_cost_usd: float | None, num_turns: int | None = None) -> None:
        """Attach provider-reported cost and turn count to the trajectory.

        Called by the SDK translator when a ``ResultMessage`` arrives.
        Both values land verbatim on the finalized :class:`Trajectory`;
        re-calling this overwrites earlier values (the SDK can emit
        multiple ResultMessages on retry, and the last one wins).
        """
        self._total_cost_usd = total_cost_usd
        self._num_turns = num_turns

    def finish(self) -> Trajectory:
        return Trajectory(
            task_id=self._task_id,
            configuration_name=self._configuration_name,
            started_at=self._started_at,
            completed_at=_now(),
            events=list(self._events),
            final_answer=self._final_answer,
            total_cost_usd=self._total_cost_usd,
            num_turns=self._num_turns,
        )


__all__ = [
    "AssistantTurn",
    "FinalAnswer",
    "ToolResult",
    "ToolUse",
    "Trajectory",
    "TrajectoryEvent",
    "TrajectoryRecorder",
    "UserMessage",
]

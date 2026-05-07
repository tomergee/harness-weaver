"""Task: the unit of work the harness runs.

A :class:`Task` is JSON-loadable and self-describing: it carries the user's
prompt, optional user context, an expected-outcome description for the
judge, and free-form tags. Task packs are just lists of tasks.
"""

import json
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(description="Stable identifier; used in output filenames.")
    user_prompt: str = Field(description="The natural-language request the agent receives.")
    user_id: str | None = Field(
        default=None,
        description="If set, tools that need user context (e.g. user_history) can use this.",
    )
    expected_outcome: str | None = Field(
        default=None,
        description="Free-text description of what a good answer looks like. Read by the judge.",
    )
    success_criteria: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional structured criteria (e.g. min_results, max_runtime_minutes).",
    )
    tags: list[str] = Field(default_factory=list)

    @classmethod
    def from_path(cls, path: Path) -> Self:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class TaskPack(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    tasks: list[Task]

    @classmethod
    def from_path(cls, path: Path) -> Self:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate(raw)


__all__ = ["Task", "TaskPack"]

"""Tool protocol shared by every callable surface the agent can use.

A :class:`Tool` couples three things: a stable name, a pydantic input model
(which doubles as the JSON schema we hand to MCP), and an ``execute`` method
that runs the actual logic. The ``call`` helper validates a raw arguments
dict against the input model and serializes the result, which is what an
MCP transport ultimately needs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Generic, TypeVar

from pydantic import BaseModel, ValidationError

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class ToolError(Exception):
    """Raised when a tool's input fails validation or execution fails predictably.

    Tools should let unexpected exceptions propagate; ``ToolError`` is for
    cases where the tool wants to return a structured error to the caller
    (e.g. "no such movie", "unknown user").
    """


class Tool(ABC, Generic[InputT, OutputT]):
    """Abstract base for every tool exposed to the agent.

    Subclasses declare the input/output models as class attributes and
    implement :meth:`execute`. ``call`` and ``input_schema`` work for free.
    """

    name: ClassVar[str]
    description: ClassVar[str]
    input_model: type[InputT]
    output_model: type[OutputT]

    @abstractmethod
    def execute(self, args: InputT) -> OutputT:
        """Run the tool with already-validated input."""

    def call(self, arguments: dict[str, object]) -> dict[str, object]:
        """Validate raw arguments, run the tool, return JSON-serializable output."""
        try:
            validated = self.input_model.model_validate(arguments)
        except ValidationError as e:
            raise ToolError(f"invalid arguments for {self.name!r}: {e}") from e
        result = self.execute(validated)
        return result.model_dump(mode="json")

    def input_schema(self) -> dict[str, object]:
        """JSON schema for this tool's input. Suitable for MCP tool listing."""
        return self.input_model.model_json_schema()


__all__ = ["InputT", "OutputT", "Tool", "ToolError"]

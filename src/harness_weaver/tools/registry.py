"""Registry that holds the active tool set for a Harness run.

The registry is what the MCP server iterates over to advertise tools and
what it dispatches to on a tool call. It is also what configurations bind
when they decide which tools are available to a given agent.

Generics are erased at the registry boundary: tools in a registry are
heterogeneous (different input/output models), and the only operations the
registry performs go through ``call(arguments)`` and ``input_schema()``,
which are dict-typed. ``Tool[Any, Any]`` is the right shape here.
"""

from collections.abc import Iterable, Iterator
from typing import Any

from harness_weaver.tools.base import Tool


class ToolRegistry:
    """Mutable name → :class:`Tool` mapping with safe registration semantics."""

    def __init__(self, tools: Iterable[Tool[Any, Any]] | None = None) -> None:
        self._tools: dict[str, Tool[Any, Any]] = {}
        if tools is not None:
            for t in tools:
                self.register(t)

    def register(self, tool: Tool[Any, Any]) -> None:
        """Add a tool. Reject duplicate names — silent shadowing is a footgun."""
        if tool.name in self._tools:
            raise ValueError(
                f"tool {tool.name!r} already registered "
                f"(existing={type(self._tools[tool.name]).__name__}, "
                f"new={type(tool).__name__})"
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool[Any, Any] | None:
        return self._tools.get(name)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def __iter__(self) -> Iterator[Tool[Any, Any]]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    @property
    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def call(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        """Dispatch a tool call by name."""
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"no tool named {name!r}; have {self.names}")
        return tool.call(arguments)

    def subset(self, names: Iterable[str]) -> "ToolRegistry":
        """Return a new registry containing only the named tools.

        Used by configurations that scope a worker agent's tool surface.
        Unknown names are an error rather than silently dropped.
        """
        wanted = list(names)
        missing = [n for n in wanted if n not in self._tools]
        if missing:
            raise KeyError(f"unknown tools in subset: {missing}")
        out = ToolRegistry()
        for n in wanted:
            out.register(self._tools[n])
        return out


__all__ = ["ToolRegistry"]

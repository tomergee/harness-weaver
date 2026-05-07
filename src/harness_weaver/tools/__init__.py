"""Tool layer.

A :class:`Tool` is a typed, MCP-shaped operation: name, description, JSON
schema for inputs, and an :meth:`~Tool.execute` method that takes a validated
pydantic model and returns a pydantic model. The MCP server (``mcp_server``
module) is the *transport* that exposes these tools; tools themselves know
nothing about MCP.

Catalog tools live here. The dangerous ``run_python`` tool — when added —
will sit in ``tools.sandbox`` because it depends on the execution backend.
"""

from __future__ import annotations

from harness_weaver.tools.base import Tool, ToolError
from harness_weaver.tools.catalog_tools import (
    GetMetadataTool,
    SearchTitlesTool,
    UserHistoryTool,
)
from harness_weaver.tools.registry import ToolRegistry
from harness_weaver.tools.sandbox import RunPythonTool

__all__ = [
    "GetMetadataTool",
    "RunPythonTool",
    "SearchTitlesTool",
    "Tool",
    "ToolError",
    "ToolRegistry",
    "UserHistoryTool",
]

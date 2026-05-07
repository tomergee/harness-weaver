"""SDK MCP server wrapping a :class:`ToolRegistry`.

This module bridges our typed-Python tool layer to the Claude Agent SDK's
in-process MCP transport. Each :class:`harness_weaver.tools.Tool` in the
registry is wrapped as an ``SdkMcpTool`` and combined into an
``McpSdkServerConfig`` that ``ClaudeAgentOptions.mcp_servers`` accepts.

We use the **in-process** server (``create_sdk_mcp_server``) rather than a
stdio subprocess because:

* The tools share the same :class:`Catalog` and :class:`ExecutionBackend`
  the harness already constructed — no IPC, no need to re-load the catalog
  in a child process.
* Trajectory recording stays simple: the SDK's hooks fire in the same
  process the harness runs in, so there's nothing to ship across a pipe.
* It still exercises the MCP boundary (schemas, JSON in/out, error shape),
  which is the part the README's design notes care about.

If the project ever needs a real out-of-process sandbox for tools, this
module is the seam to swap — produce an ``McpStdioServerConfig`` instead.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import claude_agent_sdk as sdk

from harness_weaver.tools.base import ToolError

if TYPE_CHECKING:
    from harness_weaver.tools.base import Tool
    from harness_weaver.tools.registry import ToolRegistry

DEFAULT_SERVER_NAME = "harness_weaver"


def qualified_tool_name(tool_name: str, server_name: str = DEFAULT_SERVER_NAME) -> str:
    """The SDK exposes MCP tools to the model under ``mcp__<server>__<tool>``.

    ``ClaudeAgentOptions.allowed_tools`` and ``AgentDefinition.tools`` must
    use the qualified form, otherwise the SDK treats every tool call as
    unpermitted and the agent gives up. Verified empirically against
    claude-agent-sdk 0.1.76 — see the smoke run that surfaced this in
    PR-3 review.
    """
    return f"mcp__{server_name}__{tool_name}"


def wrap_tools(registry: ToolRegistry) -> list[sdk.SdkMcpTool[Any]]:
    """Return the SDK-tool wrappers for every tool in the registry.

    Exposed separately from :func:`build_sdk_server` so tests can dispatch
    a wrapped tool directly without poking at SDK Server internals.
    """
    return [_wrap_tool(t) for t in registry]


def build_sdk_server(
    registry: ToolRegistry,
    *,
    name: str = DEFAULT_SERVER_NAME,
    version: str = "1.0.0",
) -> sdk.McpSdkServerConfig:
    """Build an in-process SDK MCP server that exposes every tool in ``registry``.

    The returned config goes into :attr:`ClaudeAgentOptions.mcp_servers`
    keyed by ``name``. Tool dispatch from the SDK to our Tool layer happens
    via the wrapper coroutines built here — each one calls ``Tool.call``
    and packages the JSON result as MCP ``content``.
    """
    return sdk.create_sdk_mcp_server(name=name, version=version, tools=wrap_tools(registry))


def _wrap_tool(tool: Tool[Any, Any]) -> sdk.SdkMcpTool[Any]:
    """Adapt one of our :class:`Tool` instances to an SDK ``SdkMcpTool``.

    The SDK's ``@tool`` decorator wants:
      * ``name``, ``description`` — string metadata.
      * ``input_schema`` — a JSON Schema dict; pydantic gives us this for free.
      * an async function ``(args: dict) -> {"content": [...]}``.

    Our Tool returns a JSON-serializable dict; we wrap it as a single text
    content block so MCP can carry it. ``ToolError`` is surfaced with
    ``is_error: True`` so Claude can reason about the failure rather than
    have the whole tool call appear to silently succeed with junk.
    """
    schema = tool.input_schema()
    name = tool.name
    description = tool.description

    @sdk.tool(name, description, schema)
    async def wrapper(args: dict[str, Any]) -> dict[str, Any]:
        # ``Tool.call`` is sync. The most expensive call we ship —
        # ``run_python`` via ``LocalSubprocessBackend`` — blocks for up to
        # ``timeout_seconds``. Running it on the event loop would freeze
        # everything else (network reads from the SDK, hook events) for the
        # full duration. Offload to a worker thread so the loop stays
        # responsive. ``asyncio.to_thread`` is a no-op cost for the cheap
        # tools (catalog reads complete in microseconds), so always-thread
        # is the right default.
        try:
            result = await asyncio.to_thread(tool.call, args)
        except ToolError as exc:
            return {
                "content": [{"type": "text", "text": str(exc)}],
                "is_error": True,
            }
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    return wrapper


__all__ = ["DEFAULT_SERVER_NAME", "build_sdk_server", "qualified_tool_name", "wrap_tools"]

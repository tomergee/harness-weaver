"""Compile a harness :class:`Configuration` into ``ClaudeAgentOptions``.

This is the one-way function from "what should this run look like" (our
declarative Configuration) to "what does the SDK need to know" (its
imperative options object). Keeping it pure and isolated makes the
mapping testable without spinning up a real session.

Mapping:

* ``Configuration.system_prompt``   → ``ClaudeAgentOptions.system_prompt``
* ``Configuration.allowed_tools``   → ``allowed_tools`` (auto-permitted)
* ``Configuration.agents``          → ``agents`` (SDK ``AgentDefinition`` map)
* the supplied MCP server           → ``mcp_servers`` keyed by ``server_name``
* built-in tools (Bash, Read, ...)  → disabled (``tools=[]``)
* permission prompts                → bypassed (non-interactive runs)

Tool naming convention: the SDK exposes MCP tools to the model under the
prefix ``mcp__<server_name>__<tool_name>``, but its public ``allowed_tools``
field accepts the bare names — see the calculator example in
``create_sdk_mcp_server.__doc__``. We pass bare names through.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import claude_agent_sdk as sdk

from harness_weaver.mcp_server import DEFAULT_SERVER_NAME

if TYPE_CHECKING:
    from harness_weaver.configurations import Configuration


def compile_options(
    configuration: Configuration,
    *,
    mcp_server: sdk.McpSdkServerConfig,
    server_name: str = DEFAULT_SERVER_NAME,
) -> sdk.ClaudeAgentOptions:
    """Translate a :class:`Configuration` into ``ClaudeAgentOptions``.

    ``mcp_server`` is the value :func:`build_sdk_server` produced for the
    same registry the harness will dispatch through. The two have to be
    constructed together — the SDK looks up tools by name in the server,
    so a configuration referring to a tool the server doesn't expose
    fails at runtime when the model tries to call it.
    """
    agent_map = {
        agent.role_name: sdk.AgentDefinition(
            description=f"Worker agent: {agent.role_name}",
            prompt=agent.system_prompt,
            tools=list(agent.allowed_tools),
        )
        for agent in configuration.agents
    }
    return sdk.ClaudeAgentOptions(
        system_prompt=configuration.system_prompt,
        # Disable all built-in Claude Code tools; the catalog harness only
        # uses tools we expose through the MCP server.
        tools=[],
        allowed_tools=list(configuration.allowed_tools),
        mcp_servers={server_name: mcp_server},
        agents=agent_map or None,
        # Non-interactive run — never prompt the user for tool approval.
        permission_mode="bypassPermissions",
    )


__all__ = ["compile_options"]

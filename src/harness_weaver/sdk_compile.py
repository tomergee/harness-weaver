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

from harness_weaver.mcp_server import DEFAULT_SERVER_NAME, qualified_tool_name

if TYPE_CHECKING:
    from harness_weaver.configurations import Configuration

DELEGATION_TOOL_NAME = "Agent"
"""Tool name the SDK uses for orchestrator → worker delegation. The
:mod:`harness_weaver.sdk_translate` module imports this so role
attribution stays in sync if the SDK renames it."""


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

    Tool names in ``allowed_tools`` and ``AgentDefinition.tools`` are
    qualified with the ``mcp__<server>__`` prefix because that's the
    namespaced form the SDK exposes MCP tools under; bare names are
    silently treated as unpermitted by ``permission_mode="default"``.
    """
    agent_map = {
        agent.role_name: sdk.AgentDefinition(
            description=f"Worker agent: {agent.role_name}",
            prompt=agent.system_prompt,
            tools=[qualified_tool_name(t, server_name) for t in agent.allowed_tools],
        )
        for agent in configuration.agents
    }
    # Built-in tools we keep enabled. The catalog harness has no use for
    # Bash/Read/Edit/etc., but multi-agent configurations need ``Task`` so
    # the orchestrator can delegate to workers via ADR-0002's hierarchical
    # subagent model. Without this, ``tools=[]`` disables Task too and the
    # orchestrator silently falls back to calling catalog tools itself
    # (verified empirically — the multi-agent run before this change had
    # zero successful tool calls, with the orchestrator getting permission
    # errors on every attempt).
    # claude-agent-sdk 0.1.76 exposes the delegation tool as ``Agent``;
    # older Claude Code versions used ``Task``. Pulled into a constant so
    # the translator can match the same name when attributing subsequent
    # subagent messages.
    builtin_tools: list[str] = [DELEGATION_TOOL_NAME] if configuration.is_multi_agent else []

    # Multi-agent allow-list has to include:
    #   * the delegation tool itself,
    #   * every tool any worker is allowed to call — workers' calls route
    #     through the top-level ``allowed_tools`` for permission decisions
    #     even when ``AgentDefinition.tools`` declares them. Without this
    #     union, every worker tool call comes back permission-denied.
    permitted_tool_set: set[str] = {
        qualified_tool_name(t, server_name) for t in configuration.allowed_tools
    }
    for agent in configuration.agents:
        permitted_tool_set.update(qualified_tool_name(t, server_name) for t in agent.allowed_tools)
    permitted_tools = sorted(permitted_tool_set)
    if configuration.is_multi_agent:
        permitted_tools.append(DELEGATION_TOOL_NAME)

    return sdk.ClaudeAgentOptions(
        system_prompt=configuration.system_prompt,
        tools=builtin_tools,
        allowed_tools=permitted_tools,
        mcp_servers={server_name: mcp_server},
        agents=agent_map or None,
        # ``default`` lets the SDK auto-permit anything in ``allowed_tools`` while
        # still rejecting anything not on the list. ``bypassPermissions`` would
        # be cleaner conceptually but the underlying CLI refuses that flag when
        # the harness runs as root, so we stay with the looser-feeling-but-
        # actually-equivalent ``default`` mode.
        permission_mode="default",
        # When None, the SDK picks its default; when set, pin the model.
        model=configuration.model,
        # Suppress Claude Code's project/user/local context injection — without
        # this, the model receives the host repo's git status, settings files,
        # etc. as additional system context and gets confused (e.g. starts
        # reasoning about uncommitted git changes inside a movie-recommendation
        # task). Verified empirically; the trajectories before this setting was
        # added contained "I see a git/repository management message there" and
        # similar leakage.
        setting_sources=[],
        skills=[],
    )


__all__ = ["compile_options"]

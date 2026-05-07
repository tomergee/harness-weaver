"""Unit tests for the Configuration → ClaudeAgentOptions compiler.

This is a pure mapping function; tests are structural assertions about
the resulting options object. Behavioral tests live alongside
``RealAgentRunner`` in ``test_real_agent_runner.py``.
"""

import claude_agent_sdk as sdk

from harness_weaver.configurations import (
    MULTI_AGENT_DISCOVERY_EXPLAINER,
    SINGLE_AGENT_BASIC,
    SINGLE_AGENT_WITH_SANDBOX,
)
from harness_weaver.mcp_server import DEFAULT_SERVER_NAME, build_sdk_server
from harness_weaver.sdk_compile import compile_options
from harness_weaver.tools import ToolRegistry


def _empty_server() -> sdk.McpSdkServerConfig:
    return build_sdk_server(ToolRegistry())


class TestSingleAgentMapping:
    def test_options_match_configuration(self) -> None:
        server = _empty_server()
        opts = compile_options(SINGLE_AGENT_BASIC, mcp_server=server)

        assert opts.system_prompt == SINGLE_AGENT_BASIC.system_prompt
        # Tool names get the SDK's MCP prefix; the model sees them as
        # mcp__harness_weaver__<name> and the allow-list must agree.
        # Order doesn't matter for permissioning; we sort + dedupe for
        # determinism.
        assert set(opts.allowed_tools) == {
            "mcp__harness_weaver__search_titles",
            "mcp__harness_weaver__get_metadata",
            "mcp__harness_weaver__user_history",
        }
        # Single-agent: no built-in tools, no delegation tool.
        assert opts.tools == []
        assert opts.agents is None  # single-agent has no workers

    def test_mcp_server_keyed_by_default_name(self) -> None:
        server = _empty_server()
        opts = compile_options(SINGLE_AGENT_BASIC, mcp_server=server)
        assert isinstance(opts.mcp_servers, dict)
        assert DEFAULT_SERVER_NAME in opts.mcp_servers
        assert opts.mcp_servers[DEFAULT_SERVER_NAME] is server

    def test_custom_server_name(self) -> None:
        server = _empty_server()
        opts = compile_options(SINGLE_AGENT_BASIC, mcp_server=server, server_name="alt")
        assert isinstance(opts.mcp_servers, dict)
        assert "alt" in opts.mcp_servers
        assert DEFAULT_SERVER_NAME not in opts.mcp_servers

    def test_permission_mode_uses_default(self) -> None:
        # ``default`` auto-permits anything in allowed_tools; ``bypassPermissions``
        # is rejected by the underlying CLI when running as root.
        opts = compile_options(SINGLE_AGENT_BASIC, mcp_server=_empty_server())
        assert opts.permission_mode == "default"

    def test_with_sandbox_includes_run_python(self) -> None:
        opts = compile_options(SINGLE_AGENT_WITH_SANDBOX, mcp_server=_empty_server())
        assert "mcp__harness_weaver__run_python" in opts.allowed_tools

    def test_model_pin_propagates_when_set(self) -> None:
        pinned = SINGLE_AGENT_BASIC.model_copy(update={"model": "claude-haiku-4-5-20251001"})
        opts = compile_options(pinned, mcp_server=_empty_server())
        assert opts.model == "claude-haiku-4-5-20251001"

    def test_model_none_means_sdk_default(self) -> None:
        # SINGLE_AGENT_BASIC.model is None by default; the SDK option should
        # also be None so the SDK picks its own default.
        assert SINGLE_AGENT_BASIC.model is None
        opts = compile_options(SINGLE_AGENT_BASIC, mcp_server=_empty_server())
        assert opts.model is None


class TestMultiAgentMapping:
    def test_workers_become_agent_definitions(self) -> None:
        opts = compile_options(MULTI_AGENT_DISCOVERY_EXPLAINER, mcp_server=_empty_server())
        assert opts.agents is not None
        assert set(opts.agents.keys()) == {"discovery", "explainer"}

    def test_worker_prompts_propagate(self) -> None:
        opts = compile_options(MULTI_AGENT_DISCOVERY_EXPLAINER, mcp_server=_empty_server())
        assert opts.agents is not None
        discovery = opts.agents["discovery"]
        assert discovery.prompt == MULTI_AGENT_DISCOVERY_EXPLAINER.agents[0].system_prompt

    def test_worker_tool_surfaces_propagate(self) -> None:
        opts = compile_options(MULTI_AGENT_DISCOVERY_EXPLAINER, mcp_server=_empty_server())
        assert opts.agents is not None
        explainer = opts.agents["explainer"]
        # Worker tool names are also qualified with the MCP server prefix.
        assert explainer.tools == ["mcp__harness_weaver__get_metadata"]

    def test_worker_descriptions_are_set(self) -> None:
        # AgentDefinition.description is required; we synthesize a sensible one.
        opts = compile_options(MULTI_AGENT_DISCOVERY_EXPLAINER, mcp_server=_empty_server())
        assert opts.agents is not None
        for role, agent in opts.agents.items():
            assert role in agent.description

    def test_multi_agent_includes_delegation_tool(self) -> None:
        # Without the delegation tool (``Agent`` in claude-agent-sdk 0.1.76),
        # the orchestrator can't delegate — and silently falls back to
        # calling MCP tools directly with permission errors.
        opts = compile_options(MULTI_AGENT_DISCOVERY_EXPLAINER, mcp_server=_empty_server())
        assert opts.tools == ["Agent"]
        assert "Agent" in opts.allowed_tools

    def test_single_agent_does_not_include_delegation_tool(self) -> None:
        opts = compile_options(SINGLE_AGENT_BASIC, mcp_server=_empty_server())
        assert "Agent" not in opts.tools
        assert "Agent" not in opts.allowed_tools

    def test_multi_agent_unions_worker_tools_into_allowed_tools(self) -> None:
        # Worker tool calls route through the top-level allowed_tools for
        # permission, even though AgentDefinition.tools declares them.
        # Without this union, the workers can't actually call anything.
        opts = compile_options(MULTI_AGENT_DISCOVERY_EXPLAINER, mcp_server=_empty_server())
        # Discovery worker has search_titles, get_metadata, user_history.
        # Explainer worker has get_metadata. Top-level should have all three
        # MCP-qualified, plus Agent.
        assert "mcp__harness_weaver__search_titles" in opts.allowed_tools
        assert "mcp__harness_weaver__get_metadata" in opts.allowed_tools
        assert "mcp__harness_weaver__user_history" in opts.allowed_tools

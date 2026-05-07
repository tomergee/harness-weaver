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
        assert opts.allowed_tools == list(SINGLE_AGENT_BASIC.allowed_tools)
        assert opts.tools == []  # built-ins disabled
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

    def test_permission_mode_bypasses_prompts(self) -> None:
        opts = compile_options(SINGLE_AGENT_BASIC, mcp_server=_empty_server())
        # Non-interactive runs must not prompt for tool approval.
        assert opts.permission_mode == "bypassPermissions"

    def test_with_sandbox_includes_run_python(self) -> None:
        opts = compile_options(SINGLE_AGENT_WITH_SANDBOX, mcp_server=_empty_server())
        assert "run_python" in opts.allowed_tools


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
        assert explainer.tools == ["get_metadata"]

    def test_worker_descriptions_are_set(self) -> None:
        # AgentDefinition.description is required; we synthesize a sensible one.
        opts = compile_options(MULTI_AGENT_DISCOVERY_EXPLAINER, mcp_server=_empty_server())
        assert opts.agents is not None
        for role, agent in opts.agents.items():
            assert role in agent.description

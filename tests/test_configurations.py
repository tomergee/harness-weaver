"""Unit tests for the Configuration model and built-ins."""

import pytest

from harness_weaver.configurations import (
    MULTI_AGENT_DISCOVERY_EXPLAINER,
    SINGLE_AGENT_BASIC,
    SINGLE_AGENT_WITH_SANDBOX,
    AgentDefinition,
    Configuration,
    builtin_configurations,
    configuration_by_name,
)


class TestBuiltins:
    def test_three_built_ins_registered(self) -> None:
        names = [c.name for c in builtin_configurations()]
        assert names == [
            "single-agent-basic",
            "single-agent-with-sandbox",
            "multi-agent-discovery-explainer",
        ]

    def test_lookup_by_name(self) -> None:
        assert configuration_by_name("single-agent-basic") is SINGLE_AGENT_BASIC

    def test_lookup_unknown_lists_options(self) -> None:
        with pytest.raises(KeyError, match="single-agent-basic"):
            configuration_by_name("not-a-real-config")

    def test_basic_is_single_agent(self) -> None:
        assert SINGLE_AGENT_BASIC.is_multi_agent is False
        assert SINGLE_AGENT_BASIC.agents == ()

    def test_with_sandbox_adds_run_python(self) -> None:
        assert "run_python" not in SINGLE_AGENT_BASIC.allowed_tools
        assert "run_python" in SINGLE_AGENT_WITH_SANDBOX.allowed_tools

    def test_multi_agent_has_two_workers(self) -> None:
        assert MULTI_AGENT_DISCOVERY_EXPLAINER.is_multi_agent
        roles = [a.role_name for a in MULTI_AGENT_DISCOVERY_EXPLAINER.agents]
        assert roles == ["discovery", "explainer"]

    def test_explainer_has_narrower_tool_surface(self) -> None:
        # ADR-0002 — workers can be scoped to subsets of tools.
        agents = {a.role_name: a for a in MULTI_AGENT_DISCOVERY_EXPLAINER.agents}
        assert set(agents["explainer"].allowed_tools).issubset(
            set(agents["discovery"].allowed_tools)
        )


class TestValidation:
    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValueError, match="extra"):
            Configuration(  # type: ignore[call-arg]
                name="x",
                description="x",
                system_prompt="x",
                allowed_tools=("search_titles",),
                bogus="field",
            )

    def test_agent_extra_field_rejected(self) -> None:
        with pytest.raises(ValueError, match="extra"):
            AgentDefinition(  # type: ignore[call-arg]
                role_name="r",
                system_prompt="s",
                allowed_tools=(),
                bogus="x",
            )

    def test_frozen(self) -> None:
        cfg = SINGLE_AGENT_BASIC
        with pytest.raises(ValueError, match="frozen"):
            cfg.name = "different"  # type: ignore[misc]

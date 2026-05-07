"""Configurations: the unit of variation across runs.

A :class:`Configuration` bundles everything that differs between two
otherwise-identical runs: system prompt, allowed tools, and (for
multi-agent runs) the worker definitions. The model, temperature, and
random seed are held constant outside this layer, so two trajectories
produced by the same task on different configurations differ in
exactly the thing the configuration says they should.

Three built-in configurations ship with the package:

* ``single-agent-basic``           catalog tools only.
* ``single-agent-with-sandbox``    catalog tools + ``run_python``.
* ``multi-agent-discovery-explainer``  orchestrator → Discovery → Explainer.

Add new configurations by constructing a :class:`Configuration` and
registering it with :func:`register_configuration`, or load one from
JSON with :meth:`Configuration.from_path`.
"""

import json
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field


class AgentDefinition(BaseModel):
    """Worker agent inside a multi-agent configuration.

    Each worker has its own scoped system prompt and tool surface. ADR-0002
    describes the topology: workers are launched by the orchestrator via
    the SDK's ``Task`` tool, never peer-to-peer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    role_name: str = Field(description="Stable identifier for this worker, e.g. 'discovery'.")
    system_prompt: str
    allowed_tools: tuple[str, ...] = Field(
        description="Names of tools this worker may invoke. Must be a subset of registered tools."
    )


class Configuration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    system_prompt: str
    allowed_tools: tuple[str, ...] = Field(
        description=(
            "Tools the orchestrator may invoke directly. For multi-agent configurations "
            "this is typically just the delegation tool ('Task') plus anything the "
            "orchestrator should not have to delegate."
        )
    )
    agents: tuple[AgentDefinition, ...] = Field(
        default=(),
        description="Worker definitions. Empty for single-agent configurations.",
    )

    @property
    def is_multi_agent(self) -> bool:
        return bool(self.agents)

    @classmethod
    def from_path(cls, path: Path) -> Self:
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))


# --- built-ins -----------------------------------------------------------


SINGLE_AGENT_BASIC = Configuration(
    name="single-agent-basic",
    description=(
        "Single agent with the catalog tools (search_titles, get_metadata, user_history). "
        "The baseline against which every other configuration is compared."
    ),
    system_prompt=(
        "You are a film recommendation assistant. The user will ask for movie suggestions; "
        "use the catalog tools to find candidates and ground every recommendation in "
        "concrete catalog data. Always justify why a title fits the user's request, "
        "referencing facts the tools returned. Do not invent titles, ratings, or runtimes."
    ),
    allowed_tools=("search_titles", "get_metadata", "user_history"),
)


SINGLE_AGENT_WITH_SANDBOX = Configuration(
    name="single-agent-with-sandbox",
    description=(
        "Same as single-agent-basic, plus run_python for sandboxed code execution. "
        "Use this when comparing whether code execution improves analytical queries."
    ),
    system_prompt=(
        "You are a film recommendation assistant with access to a sandboxed Python "
        "environment. For analytical queries (filtering, sorting, ranking by multiple "
        "criteria), prefer using run_python to compute the answer over data the catalog "
        "tools have returned, rather than reasoning about it textually. Always justify "
        "recommendations with catalog facts and do not invent titles, ratings, or runtimes."
    ),
    allowed_tools=("search_titles", "get_metadata", "user_history", "run_python"),
)


MULTI_AGENT_DISCOVERY_EXPLAINER = Configuration(
    name="multi-agent-discovery-explainer",
    description=(
        "Orchestrator delegates discovery to a Discovery worker (full catalog access) "
        "and presentation to an Explainer worker (metadata only). Tests whether splitting "
        "the work across specialist workers improves recommendation quality."
    ),
    system_prompt=(
        "You are a recommendation orchestrator. Decompose the user's request into a "
        "discovery step and an explanation step. Delegate the search to the Discovery "
        "worker, then ask the Explainer worker to write the user-facing justification "
        "based on the candidates Discovery returned."
    ),
    allowed_tools=(),  # orchestrator only delegates
    agents=(
        AgentDefinition(
            role_name="discovery",
            system_prompt=(
                "You are Discovery. Given a recommendation request, find a small set of "
                "candidate titles that fit the user's constraints. Use search_titles, "
                "get_metadata, and user_history. Return candidates as a JSON list of "
                "movie ids — no prose."
            ),
            allowed_tools=("search_titles", "get_metadata", "user_history"),
        ),
        AgentDefinition(
            role_name="explainer",
            system_prompt=(
                "You are Explainer. Given a list of candidate movie ids and the user's "
                "original request, write a concise user-facing recommendation that names "
                "each title and justifies it with concrete metadata. Use get_metadata to "
                "retrieve facts; do not invent any."
            ),
            allowed_tools=("get_metadata",),
        ),
    ),
)


_BUILTIN: dict[str, Configuration] = {
    c.name: c
    for c in (SINGLE_AGENT_BASIC, SINGLE_AGENT_WITH_SANDBOX, MULTI_AGENT_DISCOVERY_EXPLAINER)
}


def configuration_by_name(name: str) -> Configuration:
    """Look up a built-in configuration. Unknown names list the available ones."""
    if name not in _BUILTIN:
        raise KeyError(f"unknown configuration {name!r}; have {sorted(_BUILTIN)}")
    return _BUILTIN[name]


def builtin_configurations() -> list[Configuration]:
    """Return all built-in configurations in registration order."""
    return list(_BUILTIN.values())


__all__ = [
    "MULTI_AGENT_DISCOVERY_EXPLAINER",
    "SINGLE_AGENT_BASIC",
    "SINGLE_AGENT_WITH_SANDBOX",
    "AgentDefinition",
    "Configuration",
    "builtin_configurations",
    "configuration_by_name",
]

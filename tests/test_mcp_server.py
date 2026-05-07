"""Unit tests for the SDK MCP server wrapping our ToolRegistry.

These exercise the in-process bridge: the Tool layer (ours) is wrapped
as ``SdkMcpTool`` instances, combined into an ``McpSdkServerConfig``,
and dispatched through. We don't talk to the live SDK here — we call
the wrapper coroutines directly to verify that:

* names, descriptions, and JSON schemas come through correctly,
* successful tool calls return MCP ``content`` with the JSON payload,
* :class:`~harness_weaver.tools.ToolError` surfaces as ``is_error: True``,
* unknown tools at the registry level surface as ``is_error: True``.
"""

import asyncio
import json

import pytest

from harness_weaver.catalog import Catalog, Movie, RatingEvent
from harness_weaver.mcp_server import DEFAULT_SERVER_NAME, build_sdk_server, wrap_tools
from harness_weaver.tools import (
    GetMetadataTool,
    SearchTitlesTool,
    ToolRegistry,
    UserHistoryTool,
)


@pytest.fixture
def registry() -> ToolRegistry:
    movies = [
        Movie(
            id="m1",
            title="Test Movie",
            year=2010,
            runtime_minutes=100,
            genres=["Drama"],
            rating=8.0,
            vote_count=100,
            overview="A test.",
        )
    ]
    ratings = [
        RatingEvent(user_id="user-a", movie_id="m1", rating=9.0, watched_at="2025-01-01T00:00:00Z"),  # type: ignore[arg-type]
    ]
    catalog = Catalog(movies, ratings)
    return ToolRegistry(
        [
            SearchTitlesTool(catalog),
            GetMetadataTool(catalog),
            UserHistoryTool(catalog),
        ]
    )


def _by_name(registry: ToolRegistry, name: str) -> object:
    """Pull the wrapped SdkMcpTool for ``name`` so we can dispatch it."""
    for sdk_tool in wrap_tools(registry):
        if sdk_tool.name == name:
            return sdk_tool
    raise KeyError(name)


class TestServerShape:
    def test_returns_sdk_server_config(self, registry: ToolRegistry) -> None:
        server = build_sdk_server(registry)
        assert server["type"] == "sdk"
        assert server["name"] == DEFAULT_SERVER_NAME

    def test_custom_server_name(self, registry: ToolRegistry) -> None:
        server = build_sdk_server(registry, name="custom")
        assert server["name"] == "custom"

    def test_every_registry_tool_is_wrapped(self, registry: ToolRegistry) -> None:
        wrapped = wrap_tools(registry)
        assert {t.name for t in wrapped} == {"search_titles", "get_metadata", "user_history"}

    def test_descriptions_propagate(self, registry: ToolRegistry) -> None:
        wrapped = wrap_tools(registry)
        get_meta = next(t for t in wrapped if t.name == "get_metadata")
        assert "Return full metadata" in get_meta.description


class TestToolDispatch:
    def test_successful_call_returns_text_content_with_json(self, registry: ToolRegistry) -> None:
        wrapped = _by_name(registry, "get_metadata")
        result = asyncio.run(wrapped.handler({"movie_id": "m1"}))  # type: ignore[attr-defined]

        assert isinstance(result, dict)
        assert result.get("is_error") is not True
        assert result["content"][0]["type"] == "text"
        # The JSON we wrote round-trips through the MCP text payload.
        payload = json.loads(result["content"][0]["text"])
        assert payload["movie"]["id"] == "m1"
        assert payload["movie"]["title"] == "Test Movie"

    def test_tool_error_marks_is_error_true(self, registry: ToolRegistry) -> None:
        wrapped = _by_name(registry, "get_metadata")
        result = asyncio.run(wrapped.handler({"movie_id": "does-not-exist"}))  # type: ignore[attr-defined]
        assert result["is_error"] is True
        assert "no movie with id" in result["content"][0]["text"]

    def test_invalid_arguments_also_surface_as_error(self, registry: ToolRegistry) -> None:
        # Schema validation in our Tool raises ToolError, which the wrapper
        # converts to an MCP error response.
        wrapped = _by_name(registry, "search_titles")
        result = asyncio.run(wrapped.handler({"min_rating": 99}))  # type: ignore[attr-defined]
        assert result["is_error"] is True
        assert "invalid arguments" in result["content"][0]["text"]

    def test_input_schema_is_json_schema_object(self, registry: ToolRegistry) -> None:
        wrapped = _by_name(registry, "search_titles")
        # The schema dict we passed is what pydantic produced — a JSON Schema.
        schema = wrapped.input_schema  # type: ignore[attr-defined]
        assert schema["type"] == "object"
        assert "properties" in schema

"""Unit tests for the catalog tools and registry.

Tools are tested through their MCP-shaped boundary (``call`` with a dict)
to verify both the validation layer and the underlying logic in one pass.
Where the validation behavior matters specifically, we use the typed
``execute`` path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from harness_weaver.catalog import Catalog, Movie, RatingEvent
from harness_weaver.tools import (
    GetMetadataTool,
    SearchTitlesTool,
    Tool,
    ToolError,
    ToolRegistry,
    UserHistoryTool,
)


@pytest.fixture
def small_catalog() -> Catalog:
    movies = [
        Movie(
            id="m1",
            title="Tense Thriller",
            year=2015,
            runtime_minutes=110,
            genres=["Thriller"],
            rating=8.5,
            vote_count=100,
            overview="A pulse-pounding case.",
        ),
        Movie(
            id="m2",
            title="Light Comedy",
            year=2020,
            runtime_minutes=95,
            genres=["Comedy"],
            rating=7.0,
            vote_count=50,
            overview="A breezy farce.",
        ),
    ]
    ratings = [
        RatingEvent(
            user_id="user-a",
            movie_id="m1",
            rating=9.0,
            watched_at=datetime(2025, 10, 1, tzinfo=UTC),
        ),
        RatingEvent(
            user_id="user-a",
            movie_id="m2",
            rating=6.5,
            watched_at=datetime(2025, 10, 5, tzinfo=UTC),
        ),
    ]
    return Catalog(movies, ratings)


# --- SearchTitlesTool ----------------------------------------------------


class TestSearchTitlesTool:
    def test_call_returns_serializable_dict(self, small_catalog: Catalog) -> None:
        tool = SearchTitlesTool(small_catalog)
        result = tool.call({"genres": ["Thriller"]})
        assert isinstance(result, dict)
        assert result["hits"][0]["id"] == "m1"
        assert result["total_matched"] == 1

    def test_total_matched_reflects_pre_limit_count(self, small_catalog: Catalog) -> None:
        tool = SearchTitlesTool(small_catalog)
        result = tool.call({"limit": 1})
        assert len(result["hits"]) == 1
        assert result["total_matched"] == 2

    def test_invalid_arguments_raise_tool_error(self, small_catalog: Catalog) -> None:
        tool = SearchTitlesTool(small_catalog)
        with pytest.raises(ToolError, match="invalid arguments"):
            tool.call({"min_rating": 99.0})  # > 10.0 by schema bound

    def test_extra_field_rejected(self, small_catalog: Catalog) -> None:
        tool = SearchTitlesTool(small_catalog)
        with pytest.raises(ToolError, match="invalid arguments"):
            tool.call({"bogus": "field"})

    def test_input_schema_has_required_shape(self, small_catalog: Catalog) -> None:
        schema = SearchTitlesTool(small_catalog).input_schema()
        assert schema["type"] == "object"
        assert "properties" in schema
        assert "limit" in schema["properties"]


# --- GetMetadataTool -----------------------------------------------------


class TestGetMetadataTool:
    def test_returns_full_movie_details(self, small_catalog: Catalog) -> None:
        tool = GetMetadataTool(small_catalog)
        result = tool.call({"movie_id": "m1"})
        assert result["movie"]["title"] == "Tense Thriller"
        assert result["movie"]["overview"] == "A pulse-pounding case."
        assert result["movie"]["vote_count"] == 100

    def test_unknown_movie_id_raises_tool_error(self, small_catalog: Catalog) -> None:
        tool = GetMetadataTool(small_catalog)
        with pytest.raises(ToolError, match="no movie with id"):
            tool.call({"movie_id": "m999"})

    def test_missing_movie_id_argument_rejected(self, small_catalog: Catalog) -> None:
        tool = GetMetadataTool(small_catalog)
        with pytest.raises(ToolError, match="invalid arguments"):
            tool.call({})


# --- UserHistoryTool -----------------------------------------------------


class TestUserHistoryTool:
    def test_returns_history_with_titles_joined(self, small_catalog: Catalog) -> None:
        tool = UserHistoryTool(small_catalog)
        result = tool.call({"user_id": "user-a"})
        assert result["user_id"] == "user-a"
        # most recent first
        assert [e["movie_id"] for e in result["entries"]] == ["m2", "m1"]
        # title is joined from catalog
        assert result["entries"][0]["title"] == "Light Comedy"
        assert result["total_events"] == 2

    def test_unknown_user_raises_tool_error(self, small_catalog: Catalog) -> None:
        tool = UserHistoryTool(small_catalog)
        with pytest.raises(ToolError, match="no history for user"):
            tool.call({"user_id": "nobody"})

    def test_limit_truncates(self, small_catalog: Catalog) -> None:
        tool = UserHistoryTool(small_catalog)
        result = tool.call({"user_id": "user-a", "limit": 1})
        assert len(result["entries"]) == 1
        # but total_events still reflects everything the user has watched
        assert result["total_events"] == 2

    def test_negative_limit_rejected(self, small_catalog: Catalog) -> None:
        tool = UserHistoryTool(small_catalog)
        with pytest.raises(ToolError, match="invalid arguments"):
            tool.call({"user_id": "user-a", "limit": -1})


# --- ToolRegistry --------------------------------------------------------


class TestToolRegistry:
    def test_register_and_get(self, small_catalog: Catalog) -> None:
        reg = ToolRegistry()
        tool = SearchTitlesTool(small_catalog)
        reg.register(tool)
        assert reg.get("search_titles") is tool
        assert "search_titles" in reg
        assert len(reg) == 1

    def test_duplicate_registration_rejected(self, small_catalog: Catalog) -> None:
        reg = ToolRegistry([SearchTitlesTool(small_catalog)])
        with pytest.raises(ValueError, match="already registered"):
            reg.register(SearchTitlesTool(small_catalog))

    def test_call_dispatches_by_name(self, small_catalog: Catalog) -> None:
        reg = ToolRegistry([GetMetadataTool(small_catalog)])
        result = reg.call("get_metadata", {"movie_id": "m1"})
        assert result["movie"]["id"] == "m1"

    def test_call_unknown_name_raises_keyerror(self, small_catalog: Catalog) -> None:
        reg = ToolRegistry([SearchTitlesTool(small_catalog)])
        with pytest.raises(KeyError, match="no tool named"):
            reg.call("nonexistent", {})

    def test_subset_returns_new_registry(self, small_catalog: Catalog) -> None:
        reg = ToolRegistry(
            [
                SearchTitlesTool(small_catalog),
                GetMetadataTool(small_catalog),
                UserHistoryTool(small_catalog),
            ]
        )
        subset = reg.subset(["search_titles", "get_metadata"])
        assert subset.names == ["get_metadata", "search_titles"]
        # original is untouched
        assert len(reg) == 3

    def test_subset_unknown_name_raises(self, small_catalog: Catalog) -> None:
        reg = ToolRegistry([SearchTitlesTool(small_catalog)])
        with pytest.raises(KeyError, match="unknown tools"):
            reg.subset(["bogus"])

    def test_iter_yields_registered_tools(self, small_catalog: Catalog) -> None:
        tools = [SearchTitlesTool(small_catalog), GetMetadataTool(small_catalog)]
        reg = ToolRegistry(tools)
        assert list(reg) == tools

    def test_protocol_compliance(self, small_catalog: Catalog) -> None:
        # All catalog tools satisfy the Tool ABC; this is what makes them
        # interchangeable from the registry's perspective.
        for tool_cls in (SearchTitlesTool, GetMetadataTool, UserHistoryTool):
            assert isinstance(tool_cls(small_catalog), Tool)

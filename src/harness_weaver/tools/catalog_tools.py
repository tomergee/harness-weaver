"""Catalog tools: search_titles, get_metadata, user_history.

These are pure-data tools. They take a :class:`~harness_weaver.catalog.Catalog`
in their constructor and never call out to anything dangerous, so they
don't need the execution backend.
"""

from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from harness_weaver.catalog import Catalog, SearchFilter
from harness_weaver.tools.base import Tool, ToolError

# --- search_titles -------------------------------------------------------


class SearchTitlesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None = Field(
        default=None,
        description="Free-text substring matched (case-insensitive) against title and overview.",
    )
    genres: list[str] | None = Field(
        default=None,
        description=(
            "Genre filter. A movie matches if any of its genres appears in this list "
            "(case-insensitive). Example: ['Sci-Fi', 'Thriller']."
        ),
    )
    min_year: int | None = Field(default=None, description="Earliest release year, inclusive.")
    max_year: int | None = Field(default=None, description="Latest release year, inclusive.")
    min_runtime: int | None = Field(
        default=None,
        ge=1,
        description="Minimum runtime in minutes, inclusive.",
    )
    max_runtime: int | None = Field(
        default=None,
        ge=1,
        description="Maximum runtime in minutes, inclusive.",
    )
    min_rating: float | None = Field(
        default=None,
        ge=0.0,
        le=10.0,
        description="Minimum catalog rating, inclusive (0-10 scale).",
    )
    sort_by: Literal["rating", "year", "runtime", "title"] = Field(
        default="rating",
        description="Field to sort by.",
    )
    descending: bool = Field(default=True, description="Whether to sort in descending order.")
    limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of results to return.",
    )


class SearchHit(BaseModel):
    id: str
    title: str
    year: int
    runtime_minutes: int
    genres: list[str]
    rating: float


class SearchTitlesOutput(BaseModel):
    hits: list[SearchHit]
    total_matched: int = Field(
        description="Number of catalog entries that matched the filter before `limit` was applied."
    )


class SearchTitlesTool(Tool[SearchTitlesInput, SearchTitlesOutput]):
    name: ClassVar[str] = "search_titles"
    description: ClassVar[str] = (
        "Search the movie catalog by free-text query, genre, year range, runtime range, "
        "and minimum rating. Returns up to `limit` hits sorted by `sort_by`. Use this "
        "to find candidate titles; call get_metadata for full details on a specific id."
    )
    input_model = SearchTitlesInput
    output_model = SearchTitlesOutput

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    def execute(self, args: SearchTitlesInput) -> SearchTitlesOutput:
        flt = SearchFilter(
            query=args.query,
            genres=tuple(args.genres) if args.genres is not None else None,
            min_year=args.min_year,
            max_year=args.max_year,
            min_runtime=args.min_runtime,
            max_runtime=args.max_runtime,
            min_rating=args.min_rating,
        )
        # We need both the truncated list and the full match count, so search
        # twice with different limits — cheap given the catalog size.
        all_matched = self._catalog.search(
            flt, sort_by=args.sort_by, descending=args.descending, limit=self._catalog.size or 1
        )
        hits = all_matched[: args.limit]
        return SearchTitlesOutput(
            hits=[
                SearchHit(
                    id=m.id,
                    title=m.title,
                    year=m.year,
                    runtime_minutes=m.runtime_minutes,
                    genres=m.genres,
                    rating=m.rating,
                )
                for m in hits
            ],
            total_matched=len(all_matched),
        )


# --- get_metadata --------------------------------------------------------


class GetMetadataInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    movie_id: str = Field(description="Catalog id, e.g. 'm004'. Get these from search_titles.")


class MovieDetails(BaseModel):
    id: str
    title: str
    year: int
    runtime_minutes: int
    genres: list[str]
    rating: float
    vote_count: int
    overview: str


class GetMetadataOutput(BaseModel):
    movie: MovieDetails


class GetMetadataTool(Tool[GetMetadataInput, GetMetadataOutput]):
    name: ClassVar[str] = "get_metadata"
    description: ClassVar[str] = (
        "Return full metadata for a single movie by id, including overview, vote count, "
        "and rating. Use this after search_titles to inspect a candidate in detail."
    )
    input_model = GetMetadataInput
    output_model = GetMetadataOutput

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    def execute(self, args: GetMetadataInput) -> GetMetadataOutput:
        movie = self._catalog.get(args.movie_id)
        if movie is None:
            raise ToolError(f"no movie with id {args.movie_id!r}")
        return GetMetadataOutput(
            movie=MovieDetails(
                id=movie.id,
                title=movie.title,
                year=movie.year,
                runtime_minutes=movie.runtime_minutes,
                genres=list(movie.genres),
                rating=movie.rating,
                vote_count=movie.vote_count,
                overview=movie.overview,
            )
        )


# --- user_history --------------------------------------------------------


class UserHistoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(description="Stable user id, e.g. 'user-001'.")
    limit: int | None = Field(
        default=None,
        ge=0,
        description="Maximum number of events to return, most-recent first. None means all.",
    )


class HistoryEntry(BaseModel):
    movie_id: str
    title: str
    rating: float
    watched_at: datetime


class UserHistoryOutput(BaseModel):
    user_id: str
    entries: list[HistoryEntry]
    total_events: int


class UserHistoryTool(Tool[UserHistoryInput, UserHistoryOutput]):
    name: ClassVar[str] = "user_history"
    description: ClassVar[str] = (
        "Return a user's watch history with the rating they gave each title, "
        "most-recent first. Use this to ground recommendations in what the user "
        "has actually watched."
    )
    input_model = UserHistoryInput
    output_model = UserHistoryOutput

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    def execute(self, args: UserHistoryInput) -> UserHistoryOutput:
        events = self._catalog.history_for(args.user_id, limit=args.limit)
        # If the user is unknown, history_for returns []; surface that as a
        # ToolError so the agent doesn't silently get an empty answer to a typo.
        if not events and args.user_id not in self._catalog.known_users:
            raise ToolError(f"no history for user {args.user_id!r}")
        entries: list[HistoryEntry] = []
        for ev in events:
            movie = self._catalog.get(ev.movie_id)
            assert movie is not None  # invariant enforced by Catalog at load time
            entries.append(
                HistoryEntry(
                    movie_id=ev.movie_id,
                    title=movie.title,
                    rating=ev.rating,
                    watched_at=ev.watched_at,
                )
            )
        return UserHistoryOutput(
            user_id=args.user_id,
            entries=entries,
            total_events=len(self._catalog.history_for(args.user_id)),
        )


__all__ = [
    "GetMetadataInput",
    "GetMetadataOutput",
    "GetMetadataTool",
    "HistoryEntry",
    "MovieDetails",
    "SearchHit",
    "SearchTitlesInput",
    "SearchTitlesOutput",
    "SearchTitlesTool",
    "UserHistoryInput",
    "UserHistoryOutput",
    "UserHistoryTool",
]

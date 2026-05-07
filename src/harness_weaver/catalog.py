"""Domain models and in-memory store for the movie catalog and rating events.

This is the data layer the catalog tools sit on top of. It loads two CSVs
from ``harness_weaver.data`` once and exposes typed query methods. Any tool
that wants catalog access takes a :class:`Catalog` in its constructor.
"""

import csv
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field

from harness_weaver.data import CATALOG_CSV, RATINGS_CSV


class Movie(BaseModel):
    """A single catalog entry. Field set is closed; fail loudly on extras."""

    model_config = {"frozen": True, "extra": "forbid"}

    id: str
    title: str
    year: int = Field(ge=1888)  # cinema starts roughly here
    runtime_minutes: int = Field(ge=1)
    genres: list[str]
    rating: float = Field(ge=0.0, le=10.0)
    vote_count: int = Field(ge=0)
    overview: str


class RatingEvent(BaseModel):
    """A user watching and rating a movie at a point in time."""

    model_config = {"frozen": True, "extra": "forbid"}

    user_id: str
    movie_id: str
    rating: float = Field(ge=0.0, le=10.0)
    watched_at: datetime


@dataclass(frozen=True)
class SearchFilter:
    """Composable filter for catalog search. None means "no constraint."

    Kept separate from the tool's pydantic input model so the Catalog is
    independently testable without dragging the tool layer in.

    Lowercased forms of ``query`` and ``genres`` are precomputed in
    ``__post_init__`` so the per-movie ``matches`` call doesn't redo that
    work on every iteration. The catalog also stores a precomputed
    lowercase blob per movie (see :class:`Catalog`); :meth:`matches`
    accepts it as an optional override to avoid recomputing per movie.
    """

    query: str | None = None
    genres: tuple[str, ...] | None = None
    min_year: int | None = None
    max_year: int | None = None
    min_runtime: int | None = None
    max_runtime: int | None = None
    min_rating: float | None = None
    max_rating: float | None = None

    # Precomputed in __post_init__; not part of the public surface.
    _query_lower: str | None = field(init=False, repr=False, compare=False, default=None)
    _genres_lower: frozenset[str] | None = field(
        init=False, repr=False, compare=False, default=None
    )

    def __post_init__(self) -> None:
        # Frozen dataclass: object.__setattr__ is the documented escape hatch.
        object.__setattr__(self, "_query_lower", self.query.lower() if self.query else None)
        if self.genres is not None:
            object.__setattr__(self, "_genres_lower", frozenset(g.lower() for g in self.genres))

    def matches(self, movie: Movie, *, searchable_text: str | None = None) -> bool:
        """Test a movie against this filter.

        ``searchable_text``: optional precomputed lowercase concatenation of
        title and overview. The Catalog passes this in to avoid recomputing
        per call; callers that don't have one fall back to computing it.
        """
        if self._query_lower is not None:
            haystack = searchable_text or _searchable_blob(movie)
            if self._query_lower not in haystack:
                return False
        if self._genres_lower is not None:
            have = frozenset(g.lower() for g in movie.genres)
            if not self._genres_lower & have:
                return False
        if self.min_year is not None and movie.year < self.min_year:
            return False
        if self.max_year is not None and movie.year > self.max_year:
            return False
        if self.min_runtime is not None and movie.runtime_minutes < self.min_runtime:
            return False
        if self.max_runtime is not None and movie.runtime_minutes > self.max_runtime:
            return False
        if self.min_rating is not None and movie.rating < self.min_rating:
            return False
        return not (self.max_rating is not None and movie.rating > self.max_rating)


def _searchable_blob(movie: Movie) -> str:
    """Build the lowercase haystack for substring search. Used as a fallback
    when the Catalog's precomputed index isn't available (e.g. someone calls
    ``SearchFilter.matches`` outside the Catalog)."""
    return f"{movie.title}\n{movie.overview}".lower()


SortKey = str  # "rating" | "year" | "runtime" | "title"
_SORT_KEYS: dict[str, str] = {
    "rating": "rating",
    "year": "year",
    "runtime": "runtime_minutes",
    "title": "title",
}


class Catalog:
    """Read-only in-memory store of movies and rating events.

    Loaded once at construction; query methods return new lists. The default
    constructor reads the CSVs shipped with the package; pass an explicit
    movie/rating list to drive tests with synthetic data.
    """

    def __init__(self, movies: Iterable[Movie], ratings: Iterable[RatingEvent]) -> None:
        self._movies: dict[str, Movie] = {}
        for m in movies:
            if m.id in self._movies:
                raise ValueError(f"duplicate movie id: {m.id}")
            self._movies[m.id] = m

        # Precomputed lowercase haystack per movie; used by SearchFilter.matches
        # so we don't redo `title.lower() + overview.lower()` for every search.
        self._searchable: dict[str, str] = {
            mid: _searchable_blob(m) for mid, m in self._movies.items()
        }

        self._ratings_by_user: dict[str, list[RatingEvent]] = {}
        for r in ratings:
            if r.movie_id not in self._movies:
                raise ValueError(f"rating references unknown movie: {r.movie_id}")
            self._ratings_by_user.setdefault(r.user_id, []).append(r)
        # Most-recent first within each user; stable across calls.
        for events in self._ratings_by_user.values():
            events.sort(key=lambda e: e.watched_at, reverse=True)

    @classmethod
    def load_default(cls) -> Self:
        """Load the catalog and ratings CSVs that ship with the package."""
        return cls(_read_movies(CATALOG_CSV), _read_ratings(RATINGS_CSV))

    @property
    def size(self) -> int:
        return len(self._movies)

    @property
    def known_users(self) -> list[str]:
        return sorted(self._ratings_by_user.keys())

    def get(self, movie_id: str) -> Movie | None:
        return self._movies.get(movie_id)

    def all_movies(self) -> Iterator[Movie]:
        return iter(self._movies.values())

    def search(
        self,
        criteria: SearchFilter,
        *,
        sort_by: SortKey = "rating",
        descending: bool = True,
        limit: int = 10,
    ) -> list[Movie]:
        if sort_by not in _SORT_KEYS:
            raise ValueError(f"unknown sort_by: {sort_by!r}. Expected one of {sorted(_SORT_KEYS)}.")
        if limit <= 0:
            raise ValueError(f"limit must be positive, got {limit}")
        attr = _SORT_KEYS[sort_by]
        matched = [
            m
            for mid, m in self._movies.items()
            if criteria.matches(m, searchable_text=self._searchable[mid])
        ]
        matched.sort(key=lambda m: getattr(m, attr), reverse=descending)
        return matched[:limit]

    def history_for(self, user_id: str, *, limit: int | None = None) -> list[RatingEvent]:
        events = self._ratings_by_user.get(user_id, [])
        if limit is None:
            return list(events)
        if limit < 0:
            raise ValueError(f"limit must be non-negative, got {limit}")
        return events[:limit]


def _read_movies(path: Path) -> list[Movie]:
    with path.open(newline="", encoding="utf-8") as f:
        return [
            Movie(
                id=row["id"],
                title=row["title"],
                year=int(row["year"]),
                runtime_minutes=int(row["runtime_minutes"]),
                genres=row["genres"].split("|"),
                rating=float(row["rating"]),
                vote_count=int(row["vote_count"]),
                overview=row["overview"],
            )
            for row in csv.DictReader(f)
        ]


def _read_ratings(path: Path) -> list[RatingEvent]:
    with path.open(newline="", encoding="utf-8") as f:
        return [
            RatingEvent(
                user_id=row["user_id"],
                movie_id=row["movie_id"],
                rating=float(row["rating"]),
                watched_at=datetime.fromisoformat(row["watched_at"].replace("Z", "+00:00")),
            )
            for row in csv.DictReader(f)
        ]


__all__ = ["Catalog", "Movie", "RatingEvent", "SearchFilter", "SortKey"]

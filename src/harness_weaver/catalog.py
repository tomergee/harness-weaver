"""Domain models and in-memory store for the movie catalog and rating events.

This is the data layer the catalog tools sit on top of. It loads two CSVs
from ``harness_weaver.data`` once and exposes typed query methods. Any tool
that wants catalog access takes a :class:`Catalog` in its constructor.
"""

import csv
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
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
    """

    query: str | None = None
    genres: tuple[str, ...] | None = None
    min_year: int | None = None
    max_year: int | None = None
    min_runtime: int | None = None
    max_runtime: int | None = None
    min_rating: float | None = None
    max_rating: float | None = None

    def matches(self, movie: Movie) -> bool:
        if self.query is not None:
            q = self.query.lower()
            if q not in movie.title.lower() and q not in movie.overview.lower():
                return False
        if self.genres is not None:
            wanted = {g.lower() for g in self.genres}
            have = {g.lower() for g in movie.genres}
            if not wanted & have:
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
        matched = [m for m in self._movies.values() if criteria.matches(m)]
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

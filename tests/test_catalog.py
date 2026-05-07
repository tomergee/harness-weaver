"""Unit tests for the Catalog domain layer.

These tests exercise the in-memory store directly with synthetic fixtures
and also load the bundled CSVs to confirm the default catalog is sane.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harness_weaver.catalog import Catalog, Movie, RatingEvent, SearchFilter


def _movie(id: str, **overrides: object) -> Movie:
    """Movie factory with sensible defaults; overrides win."""
    base: dict[str, object] = {
        "id": id,
        "title": f"Movie {id}",
        "year": 2000,
        "runtime_minutes": 100,
        "genres": ["Drama"],
        "rating": 7.0,
        "vote_count": 1000,
        "overview": "A test movie.",
    }
    base.update(overrides)
    return Movie(**base)  # type: ignore[arg-type]


def _rating(user: str, movie: str, *, score: float = 8.0, days_ago: int = 0) -> RatingEvent:
    ts = datetime(2025, 11, 1, tzinfo=UTC)
    if days_ago:
        ts = ts - timedelta(days=days_ago)
    return RatingEvent(user_id=user, movie_id=movie, rating=score, watched_at=ts)


# --- bundled-data smoke ---------------------------------------------------


class TestDefaultCatalog:
    def test_loads_60_movies_and_8_users(self) -> None:
        catalog = Catalog.load_default()
        assert catalog.size == 60
        assert len(catalog.known_users) == 8
        assert all(uid.startswith("user-") for uid in catalog.known_users)

    def test_every_rating_references_a_real_movie(self) -> None:
        catalog = Catalog.load_default()
        for user_id in catalog.known_users:
            for event in catalog.history_for(user_id):
                assert catalog.get(event.movie_id) is not None

    def test_runtime_distribution_spans_useful_range(self) -> None:
        # The analytical task pack will filter by runtime; the bundled data
        # needs enough spread for those filters to surface differences.
        catalog = Catalog.load_default()
        runtimes = sorted(m.runtime_minutes for m in catalog.all_movies())
        assert runtimes[0] < 90 < runtimes[-1]
        assert runtimes[-1] > 150


# --- Movie / RatingEvent validation --------------------------------------


class TestMovieValidation:
    def test_rejects_negative_year(self) -> None:
        with pytest.raises(ValueError, match="year"):
            _movie("m1", year=1800)

    def test_rejects_zero_runtime(self) -> None:
        with pytest.raises(ValueError, match="runtime_minutes"):
            _movie("m1", runtime_minutes=0)

    def test_rejects_rating_above_10(self) -> None:
        with pytest.raises(ValueError, match="rating"):
            _movie("m1", rating=11.0)

    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValueError, match="extra"):
            Movie(  # type: ignore[call-arg]
                id="m1",
                title="t",
                year=2000,
                runtime_minutes=100,
                genres=["Drama"],
                rating=7.0,
                vote_count=1,
                overview="o",
                bogus="field",
            )


# --- Catalog construction ------------------------------------------------


class TestCatalogConstruction:
    def test_duplicate_movie_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            Catalog([_movie("m1"), _movie("m1")], [])

    def test_rating_referencing_unknown_movie_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown movie"):
            Catalog([_movie("m1")], [_rating("u1", "m999")])

    def test_history_sorted_most_recent_first(self) -> None:
        catalog = Catalog(
            [_movie("m1"), _movie("m2"), _movie("m3")],
            [
                _rating("u1", "m1", days_ago=5),
                _rating("u1", "m2", days_ago=1),
                _rating("u1", "m3", days_ago=10),
            ],
        )
        history = catalog.history_for("u1")
        assert [e.movie_id for e in history] == ["m2", "m1", "m3"]


# --- search() ------------------------------------------------------------


@pytest.fixture
def varied_catalog() -> Catalog:
    return Catalog(
        [
            _movie(
                "m1",
                title="Old Sci-Fi",
                year=1970,
                runtime_minutes=100,
                genres=["Sci-Fi"],
                rating=8.0,
            ),
            _movie(
                "m2",
                title="New Sci-Fi",
                year=2020,
                runtime_minutes=140,
                genres=["Sci-Fi", "Action"],
                rating=7.5,
            ),
            _movie(
                "m3",
                title="Old Drama",
                year=1980,
                runtime_minutes=180,
                genres=["Drama"],
                rating=9.0,
            ),
            _movie(
                "m4",
                title="Short Comedy",
                year=2010,
                runtime_minutes=85,
                genres=["Comedy"],
                rating=6.5,
            ),
            _movie(
                "m5",
                title="Tense Thriller",
                year=2015,
                runtime_minutes=110,
                genres=["Thriller"],
                rating=8.5,
                overview="A pulse-pounding suspense story.",
            ),
        ],
        [],
    )


class TestSearch:
    def test_no_filter_returns_all_sorted_by_rating_desc(self, varied_catalog: Catalog) -> None:
        results = varied_catalog.search(SearchFilter(), limit=10)
        assert [m.id for m in results] == ["m3", "m5", "m1", "m2", "m4"]

    def test_genre_filter_case_insensitive(self, varied_catalog: Catalog) -> None:
        results = varied_catalog.search(SearchFilter(genres=("sci-fi",)), limit=10)
        assert {m.id for m in results} == {"m1", "m2"}

    def test_genre_filter_matches_any_genre(self, varied_catalog: Catalog) -> None:
        # m2 is Sci-Fi+Action, asking for Action should hit it
        results = varied_catalog.search(SearchFilter(genres=("Action",)), limit=10)
        assert {m.id for m in results} == {"m2"}

    def test_runtime_range(self, varied_catalog: Catalog) -> None:
        results = varied_catalog.search(SearchFilter(min_runtime=90, max_runtime=120), limit=10)
        assert {m.id for m in results} == {"m1", "m5"}

    def test_year_range_inclusive(self, varied_catalog: Catalog) -> None:
        results = varied_catalog.search(SearchFilter(min_year=1980, max_year=2010), limit=10)
        assert {m.id for m in results} == {"m3", "m4"}

    def test_min_rating(self, varied_catalog: Catalog) -> None:
        results = varied_catalog.search(SearchFilter(min_rating=8.5), limit=10)
        assert {m.id for m in results} == {"m3", "m5"}

    def test_query_matches_title_or_overview(self, varied_catalog: Catalog) -> None:
        # "Tense" only appears in title of m5
        assert {m.id for m in varied_catalog.search(SearchFilter(query="Tense"), limit=10)} == {
            "m5"
        }
        # "suspense" only appears in m5's overview
        assert {m.id for m in varied_catalog.search(SearchFilter(query="suspense"), limit=10)} == {
            "m5"
        }

    def test_combined_filters(self, varied_catalog: Catalog) -> None:
        # Sci-Fi after 2000 with runtime over 120
        results = varied_catalog.search(
            SearchFilter(genres=("Sci-Fi",), min_year=2000, min_runtime=120), limit=10
        )
        assert [m.id for m in results] == ["m2"]

    def test_sort_by_year_descending(self, varied_catalog: Catalog) -> None:
        results = varied_catalog.search(SearchFilter(), sort_by="year", descending=True, limit=10)
        assert [m.year for m in results] == sorted([m.year for m in results], reverse=True)

    def test_sort_by_runtime_ascending(self, varied_catalog: Catalog) -> None:
        results = varied_catalog.search(
            SearchFilter(), sort_by="runtime", descending=False, limit=10
        )
        assert [m.runtime_minutes for m in results] == sorted([m.runtime_minutes for m in results])

    def test_sort_by_title_alphabetical(self, varied_catalog: Catalog) -> None:
        results = varied_catalog.search(SearchFilter(), sort_by="title", descending=False, limit=10)
        titles = [m.title for m in results]
        assert titles == sorted(titles)

    def test_limit_truncates(self, varied_catalog: Catalog) -> None:
        results = varied_catalog.search(SearchFilter(), limit=2)
        assert len(results) == 2

    def test_unknown_sort_key_rejected(self, varied_catalog: Catalog) -> None:
        with pytest.raises(ValueError, match="sort_by"):
            varied_catalog.search(SearchFilter(), sort_by="bogus")

    def test_zero_limit_rejected(self, varied_catalog: Catalog) -> None:
        with pytest.raises(ValueError, match="limit"):
            varied_catalog.search(SearchFilter(), limit=0)


# --- history_for ---------------------------------------------------------


class TestHistory:
    def test_unknown_user_returns_empty(self) -> None:
        catalog = Catalog([_movie("m1")], [])
        assert catalog.history_for("nobody") == []

    def test_limit_truncates(self) -> None:
        movies = [_movie(f"m{i}") for i in range(5)]
        ratings = [_rating("u1", f"m{i}", days_ago=i) for i in range(5)]
        catalog = Catalog(movies, ratings)
        history = catalog.history_for("u1", limit=2)
        assert len(history) == 2
        # most recent two
        assert [e.movie_id for e in history] == ["m0", "m1"]

    def test_limit_zero_returns_empty(self) -> None:
        catalog = Catalog([_movie("m1")], [_rating("u1", "m1")])
        assert catalog.history_for("u1", limit=0) == []

    def test_negative_limit_rejected(self) -> None:
        catalog = Catalog([_movie("m1")], [_rating("u1", "m1")])
        with pytest.raises(ValueError, match="limit"):
            catalog.history_for("u1", limit=-1)

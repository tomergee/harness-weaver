"""Static catalog and ratings data shipped with the package.

Files:
    catalog.csv  — 60 movies with id, title, year, runtime, genres, rating, votes, overview.
    ratings.csv  — 73 (user_id, movie_id, rating, watched_at) events across 8 fictional users.

The data is hand-curated in the shape of MovieLens 100K but enriched with
runtime and overview fields that MovieLens does not provide. Sourced facts
(years, runtimes, genres) are publicly verifiable; user IDs and ratings are
fictional.
"""

from __future__ import annotations

from pathlib import Path

DATA_DIR = Path(__file__).parent
CATALOG_CSV = DATA_DIR / "catalog.csv"
RATINGS_CSV = DATA_DIR / "ratings.csv"

__all__ = ["CATALOG_CSV", "DATA_DIR", "RATINGS_CSV"]

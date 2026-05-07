"""Shared pytest fixtures.

Cassettes for vcrpy live in ``tests/cassettes/``. Tests that need to record or
replay HTTP interactions should use the ``vcr_cassette_dir`` fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def cassette_dir() -> Path:
    """Directory where vcrpy cassettes are stored."""
    return Path(__file__).parent / "cassettes"


@pytest.fixture(scope="session")
def vcr_config() -> dict[str, object]:
    """Default vcrpy configuration. Strips API keys and other secrets from recordings."""
    return {
        "filter_headers": [
            ("authorization", "DUMMY"),
            ("x-api-key", "DUMMY"),
            ("anthropic-version", "DUMMY"),
        ],
        "filter_query_parameters": [
            ("api_key", "DUMMY"),
        ],
        "record_mode": "none",  # CI: replay only. Set to "new_episodes" locally to record.
        "match_on": ["method", "scheme", "host", "port", "path", "query", "body"],
    }


@pytest.fixture
def sample_task() -> dict[str, object]:
    """A minimal valid Task dict used across tests."""
    return {
        "task_id": "test-001",
        "user_prompt": "Find me a tense thriller under two hours.",
        "user_context": {"user_id": "test-user", "history": []},
        "expected_outcome": (
            "Returns one or more thriller titles with runtime under 120 minutes; "
            "explanation references tone or pacing."
        ),
        "success_criteria": {"min_results": 1, "max_runtime_minutes": 120},
        "tags": ["discovery", "mood-based"],
    }

"""Cassette recording/replay for the SDK message stream.

Why a custom cassette layer instead of vcrpy at the HTTP boundary:
the Claude Agent SDK shells out to the ``claude`` CLI binary and
communicates over a child process. The model HTTP traffic happens
*inside* that subprocess and is invisible to vcrpy in the parent
process. Recording would silently capture nothing useful.

What we cassette instead is the SDK's *output* — the async iterator
of typed message objects (``AssistantMessage``, ``UserMessage``,
``ResultMessage``, ``SystemMessage``, etc.) that ``query()`` yields.
That's the surface our :class:`RealAgentRunner` actually consumes, so
replaying it exercises:

* configuration → ``ClaudeAgentOptions`` compilation
* in-process MCP server build from the registry
* SDK message → trajectory event translation
* recorder finalization

…without an API key in CI.

Pickle, not JSON: SDK messages are dataclasses with nested unions of
content blocks. Pickle round-trips faithfully across our pinned SDK
version. Trade-off: cassettes are opaque blobs, and they break when
the SDK ships a class rename. Both are acceptable — the cassette is
checked in, the test fails loudly on shape drift, and re-recording is
one script invocation.

Usage:

    # Record once, with ANTHROPIC_API_KEY set:
    from tests._cassette import record_query
    runner = RealAgentRunner(query_fn=record_query("tests/cassettes/foo.pkl"))
    ...

    # Replay (CI):
    from tests._cassette import replay_query
    runner = RealAgentRunner(query_fn=replay_query("tests/cassettes/foo.pkl"))
"""

from __future__ import annotations

import pickle
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import claude_agent_sdk as sdk

QueryFn = Callable[..., AsyncIterator[Any]]


def record_query(cassette_path: str | Path) -> QueryFn:
    """Return a wrapped ``query()`` that pickles every yielded message.

    The wrapper transparently delegates to ``claude_agent_sdk.query``,
    collects every message it yields into a list, and writes the list
    to ``cassette_path`` once the iterator is exhausted. Use this in a
    one-shot recording script — not in CI.
    """
    path = Path(cassette_path)

    async def _wrapped(*args: object, **kwargs: object) -> AsyncIterator[Any]:
        messages: list[Any] = []
        try:
            async for message in sdk.query(*args, **kwargs):
                messages.append(message)
                yield message
        finally:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(pickle.dumps(messages))

    return _wrapped


def replay_query(cassette_path: str | Path) -> QueryFn:
    """Return a ``query()`` substitute that yields a recorded message stream.

    Args / kwargs to the returned callable are intentionally ignored
    — the cassette captures one specific run and replays exactly that.
    For multi-run cassettes (different prompts, configurations) record
    separate files.
    """
    path = Path(cassette_path)

    async def _wrapped(*args: object, **kwargs: object) -> AsyncIterator[Any]:
        del args, kwargs
        messages: list[Any] = pickle.loads(path.read_bytes())
        for message in messages:
            yield message

    return _wrapped


__all__ = ["record_query", "replay_query"]

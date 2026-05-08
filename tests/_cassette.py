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

**Security**: ``pickle.loads`` on attacker-controlled bytes is RCE.
We mitigate via a SHA-256 integrity gate (PR #12 review): the test
file holds the expected hex digest as a constant, :func:`replay_query`
verifies it before unpickling, and any tamper (replaced cassette,
silent re-record without updating the source constant) raises
:class:`CassetteIntegrityError` instead of executing the payload.
The hash sits in source code where reviewers see it on every PR;
hash and cassette have to change together for a tamper to land.

**Atomicity**: the recorder writes the cassette only after the SDK
stream is fully consumed (PR #12 review). A mid-stream exception or
``KeyboardInterrupt`` won't leave a half-written file on disk.

Usage:

    # Record once, with ANTHROPIC_API_KEY set:
    from tests._cassette import record_query
    runner = RealAgentRunner(query_fn=record_query("tests/cassettes/foo.pkl"))
    ...
    # Then read the new cassette's sha256 from the recorder's stderr,
    # paste it into the test's EXPECTED_SHA256 constant, and commit
    # both together.

    # Replay (CI):
    from tests._cassette import replay_query
    runner = RealAgentRunner(
        query_fn=replay_query(
            "tests/cassettes/foo.pkl",
            expected_sha256="abc123...",
        ),
    )
"""

from __future__ import annotations

import hashlib
import pickle
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import claude_agent_sdk as sdk

QueryFn = Callable[..., AsyncIterator[Any]]


class CassetteIntegrityError(RuntimeError):
    """Raised when a cassette's SHA-256 doesn't match the expected value.

    Causes:
      * the cassette was tampered with on disk;
      * the cassette was re-recorded without updating the expected
        hash in the test file (legitimate, but caller should commit
        both together);
      * the cassette is corrupted.

    In all cases :func:`replay_query` refuses to ``pickle.loads`` the
    bytes — the failure surfaces as this exception instead of letting
    a malicious payload execute.
    """


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_query(cassette_path: str | Path) -> QueryFn:
    """Return a wrapped ``query()`` that pickles every yielded message.

    The wrapper transparently delegates to ``claude_agent_sdk.query``,
    collects every message it yields into a list, and — *only on
    successful completion of the stream* (PR #12 review) — writes
    the list to ``cassette_path``. A mid-stream exception or
    ``KeyboardInterrupt`` raises out before the write, leaving any
    existing cassette untouched.

    The wrapper prints the resulting cassette's SHA-256 to stderr so
    the caller can paste it into the test's ``EXPECTED_SHA256`` constant.

    Use this in a one-shot recording script — not in CI.
    """
    path = Path(cassette_path)

    async def _wrapped(*args: object, **kwargs: object) -> AsyncIterator[Any]:
        messages: list[Any] = []
        async for message in sdk.query(*args, **kwargs):
            messages.append(message)
            yield message

        # Stream completed cleanly: persist. A raised exception in the
        # loop above propagates here without writing anything.
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = pickle.dumps(messages)
        path.write_bytes(payload)
        print(
            f"cassette written: {path}\n"
            f"  sha256: {_digest(payload)}\n"
            f"  paste this into the test's EXPECTED_SHA256 constant.",
            file=sys.stderr,
        )

    return _wrapped


def replay_query(
    cassette_path: str | Path,
    *,
    expected_sha256: str,
) -> QueryFn:
    """Return a ``query()`` substitute that yields a recorded message stream.

    Verifies the cassette's SHA-256 matches ``expected_sha256``
    before unpickling (PR #12 review). The expected hash is committed
    in the calling test, so a tampered cassette (e.g. a malicious PR
    swapping the .pkl for a pickle bomb) trips the check and raises
    :class:`CassetteIntegrityError` before any pickle code runs.

    Args / kwargs to the returned callable are intentionally ignored
    — the cassette captures one specific run and replays exactly that.
    For multi-run cassettes (different prompts, configurations) record
    separate files.
    """
    path = Path(cassette_path)

    async def _wrapped(*args: object, **kwargs: object) -> AsyncIterator[Any]:
        del args, kwargs
        raw = path.read_bytes()
        actual = _digest(raw)
        if actual != expected_sha256:
            raise CassetteIntegrityError(
                f"Cassette {path} sha256 mismatch.\n"
                f"  expected: {expected_sha256}\n"
                f"  actual:   {actual}\n"
                f"Refusing to pickle.loads — the file may have been "
                f"tampered with, corrupted, or re-recorded without "
                f"updating the expected hash in the test."
            )
        messages: list[Any] = pickle.loads(raw)
        for message in messages:
            yield message

    return _wrapped


__all__ = ["CassetteIntegrityError", "record_query", "replay_query"]

"""End-to-end test that replays a recorded SDK message stream.

Closes the "honest gap" the README has been flagging: the
:class:`RealAgentRunner` pipeline (configuration → MCP → SDK
message → trajectory translation → recorder) only ran live or
against hand-stubbed messages before this. The cassette under
``tests/cassettes/`` was recorded against a real Haiku run, so this
test exercises the actual SDK message shapes the production runner
sees — without an API key in CI.

Re-record with::

    ANTHROPIC_API_KEY=sk-ant-... python scripts/record-cassette.py \\
        examples/tasks/discovery-mood-tense.json \\
        --config single-agent-basic \\
        --model claude-haiku-4-5-20251001

The recorder prints the new cassette's SHA-256 to stderr — paste it
into :data:`EXPECTED_SHA256` below and commit both the .pkl and the
test-file change together. The replay path verifies the hash before
unpickling (PR #12 review on pickle.loads); a mismatch raises
:class:`CassetteIntegrityError` instead of executing whatever the
.pkl contains.

If the test fails after a SDK upgrade, the most likely cause is that
the SDK shipped a class rename / field change. Inspect the failure
and re-record.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness_weaver.agent_runner import RealAgentRunner
from harness_weaver.catalog import Catalog
from harness_weaver.configurations import configuration_by_name
from harness_weaver.harness import Harness
from harness_weaver.task import Task
from harness_weaver.trajectory import FinalAnswer, ToolUse
from tests._cassette import CassetteIntegrityError, replay_query

CASSETTE_DIR = Path(__file__).parent / "cassettes"
CASSETTE_NAME = "discovery-mood-tense.single-agent-basic.pkl"
TASK_PATH = Path(__file__).resolve().parents[1] / "examples" / "tasks" / "discovery-mood-tense.json"

# SHA-256 of the committed cassette. Verified before any pickle.loads
# call so a tampered/replaced .pkl trips the integrity gate instead of
# being deserialized. Re-recording the cassette legitimately changes
# this value — the recorder prints the new digest to stderr; commit
# the .pkl and this constant together.
EXPECTED_SHA256 = "d297a3daf8b7814702756bd5c486de59409cbd17babe8f77f9bcbfe16a9a74a8"


@pytest.mark.e2e
def test_replay_cassette_produces_trajectory() -> None:
    """Replaying the cassette should yield a valid trajectory with the
    same task / configuration / final-answer characteristics the live
    run produced.

    This is the *deterministic* counterpart to a live ``harness-weaver
    run`` invocation: same code path, no API key, no network.
    """
    cassette = CASSETTE_DIR / CASSETTE_NAME
    if not cassette.exists():
        pytest.skip(
            f"Cassette {cassette.name} not found. Re-record with "
            f"scripts/record-cassette.py to enable this test."
        )

    task = Task.from_path(TASK_PATH)
    cfg = configuration_by_name("single-agent-basic").model_copy(
        update={"model": "claude-haiku-4-5-20251001"}
    )

    runner = RealAgentRunner(query_fn=replay_query(cassette, expected_sha256=EXPECTED_SHA256))
    harness = Harness(catalog=Catalog.load_default(), runner=runner)
    trajectory = harness.run(task, cfg)

    # Trajectory carries the inputs we fed it.
    assert trajectory.task_id == "discovery-mood-tense"
    assert trajectory.configuration_name == "single-agent-basic"

    # The cassette captured a real run that ended with a final answer.
    assert trajectory.final_answer is not None
    assert len(trajectory.final_answer) > 0

    # Recorder should have produced events. Exact count is brittle (the
    # SDK can re-shape the same prompt across versions), but presence of
    # at least one ToolUse and the FinalAnswer marker is robust.
    assert any(isinstance(ev, ToolUse) for ev in trajectory.events), (
        "Expected at least one ToolUse event in the replayed trajectory."
    )
    assert any(isinstance(ev, FinalAnswer) for ev in trajectory.events), (
        "Expected the trajectory to end with a FinalAnswer event."
    )


@pytest.mark.e2e
def test_replay_cassette_is_deterministic() -> None:
    """Running the same replay twice should produce identical events.

    The recorder doesn't care about wall-clock; only the message
    stream matters. If anything in our pipeline accidentally
    introduced nondeterminism (e.g. dict iteration order in a
    serialized field), this test would catch it.
    """
    cassette = CASSETTE_DIR / CASSETTE_NAME
    if not cassette.exists():
        pytest.skip("Cassette not present.")

    task = Task.from_path(TASK_PATH)
    cfg = configuration_by_name("single-agent-basic").model_copy(
        update={"model": "claude-haiku-4-5-20251001"}
    )

    catalog = Catalog.load_default()

    runner_a = RealAgentRunner(query_fn=replay_query(cassette, expected_sha256=EXPECTED_SHA256))
    runner_b = RealAgentRunner(query_fn=replay_query(cassette, expected_sha256=EXPECTED_SHA256))

    traj_a = Harness(catalog=catalog, runner=runner_a).run(task, cfg)
    traj_b = Harness(catalog=catalog, runner=runner_b).run(task, cfg)

    # Compare event content. The recorder stamps each event with
    # ``datetime.now()``, so timestamps necessarily differ across the
    # two runs — strip them. Everything else (event types, tool names,
    # arguments, results, content) must match byte for byte.
    def _strip_ts(events: list[object]) -> list[dict[str, object]]:
        out = []
        for ev in events:
            d = ev.model_dump()  # type: ignore[attr-defined]
            d.pop("timestamp", None)
            out.append(d)
        return out

    events_a = _strip_ts(traj_a.events)
    events_b = _strip_ts(traj_b.events)
    assert events_a == events_b
    assert traj_a.final_answer == traj_b.final_answer


def test_replay_rejects_tampered_cassette(tmp_path: Path) -> None:
    """PR #12 review: the SHA-256 integrity gate must fire before
    ``pickle.loads`` runs. A tampered .pkl (e.g. a malicious PR swapping
    the bytes for a pickle bomb) should raise ``CassetteIntegrityError``,
    not execute the payload.
    """
    import asyncio

    bad_path = tmp_path / "tampered.pkl"
    bad_path.write_bytes(b"definitely not a real pickle of SDK messages")

    iterator = replay_query(
        bad_path,
        expected_sha256="0" * 64,  # any known-bad hash; mismatch is what we test
    )()

    async def _drain() -> None:
        async for _ in iterator:
            pass

    with pytest.raises(CassetteIntegrityError):
        asyncio.run(_drain())

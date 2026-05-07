"""Unit tests for the LLM-as-judge layer.

The :class:`InspectAILlmJudge` integration is exercised with a fake
``inspect_ai.model`` Model that returns a canned ``ModelOutput``, so
these tests don't hit the API. The :class:`FixedJudge` is the simplest
implementation and serves as a reference for the protocol's contract.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from harness_weaver.judge.llm import (
    DEFAULT_JUDGE_MODEL,
    FixedJudge,
    InspectAILlmJudge,
    Judge,
    JudgeVerdict,
    _parse_verdict,
)
from harness_weaver.task import Task
from harness_weaver.trajectory import (
    FinalAnswer,
    ToolUse,
    Trajectory,
    UserMessage,
)


def _ts() -> datetime:
    return datetime(2025, 1, 1, tzinfo=UTC)


def _trajectory(*, configuration_name: str = "cfg", final_answer: str = "ok") -> Trajectory:
    return Trajectory(
        task_id="t1",
        configuration_name=configuration_name,
        started_at=_ts(),
        completed_at=_ts(),
        events=[
            UserMessage(content="recommend a thriller"),
            ToolUse(tool_name="search_titles", arguments={"genres": ["Thriller"]}),
            FinalAnswer(text=final_answer),
        ],
        final_answer=final_answer,
    )


@pytest.fixture
def task() -> Task:
    return Task(
        task_id="t1",
        user_prompt="Find me a tense thriller under two hours.",
        expected_outcome="A modern thriller, runtime < 120, justified by catalog facts.",
    )


# --- JudgeVerdict shape -------------------------------------------------


class TestJudgeVerdictModel:
    def test_winner_must_be_valid_literal(self) -> None:
        with pytest.raises(ValueError):
            JudgeVerdict(winner="maybe", reasoning="x", confidence=0.5)  # type: ignore[arg-type]

    def test_confidence_clamped(self) -> None:
        with pytest.raises(ValueError):
            JudgeVerdict(winner="a", reasoning="x", confidence=1.5)
        with pytest.raises(ValueError):
            JudgeVerdict(winner="a", reasoning="x", confidence=-0.1)

    def test_reasoning_cannot_be_empty(self) -> None:
        with pytest.raises(ValueError):
            JudgeVerdict(winner="a", reasoning="", confidence=0.5)

    def test_round_trip_through_json(self) -> None:
        v = JudgeVerdict(winner="a", reasoning="agent A grounded the answer", confidence=0.85)
        restored = JudgeVerdict.model_validate_json(v.model_dump_json())
        assert restored == v


# --- FixedJudge ---------------------------------------------------------


class TestFixedJudge:
    @pytest.mark.asyncio
    async def test_returns_canned_verdict(self, task: Task) -> None:
        judge: Judge = FixedJudge(winner="b", reasoning="canned", confidence=0.42)
        verdict = await judge.verdict(
            task=task,
            trajectory_a=_trajectory(configuration_name="a"),
            trajectory_b=_trajectory(configuration_name="b"),
        )
        assert verdict.winner == "b"
        assert verdict.reasoning == "canned"
        assert verdict.confidence == 0.42

    @pytest.mark.asyncio
    async def test_attaches_structural_report(self, task: Task) -> None:
        judge = FixedJudge()
        verdict = await judge.verdict(
            task=task,
            trajectory_a=_trajectory(configuration_name="a"),
            trajectory_b=_trajectory(configuration_name="b"),
        )
        assert verdict.structural is not None
        assert verdict.structural.task_id == "t1"


# --- _parse_verdict -----------------------------------------------------


class TestParseVerdict:
    def test_plain_json(self) -> None:
        text = json.dumps({"winner": "a", "reasoning": "A grounded better", "confidence": 0.8})
        v = _parse_verdict(text)
        assert v.winner == "a"
        assert v.confidence == 0.8

    def test_json_inside_fence_stripped(self) -> None:
        text = (
            "```json\n"
            '{"winner": "tie", "reasoning": "both gave the same answer", "confidence": 0.6}\n'
            "```"
        )
        v = _parse_verdict(text)
        assert v.winner == "tie"

    def test_unparseable_raises_with_offending_text(self) -> None:
        with pytest.raises(ValueError, match="non-JSON output"):
            _parse_verdict("not json at all")

    def test_extra_field_rejected(self) -> None:
        text = json.dumps(
            {
                "winner": "a",
                "reasoning": "ok",
                "confidence": 0.5,
                "bogus": "extra",
            }
        )
        with pytest.raises(ValueError, match="extra"):
            _parse_verdict(text)


# --- InspectAILlmJudge with a fake inspect-ai model ---------------------


class TestInspectAILlmJudge:
    @pytest.mark.asyncio
    async def test_dispatches_to_model_and_parses_verdict(
        self, task: Task, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build a fake Model whose generate() returns a canned JSON
        # completion. The judge should call generate() with the system
        # + user messages we pass and parse the JSON into a verdict.
        canned_completion = json.dumps(
            {
                "winner": "a",
                "reasoning": "A grounded its answer in catalog facts; B fabricated a runtime.",
                "confidence": 0.78,
            }
        )

        captured: dict[str, Any] = {}

        async def fake_generate(input: list[Any], **_: Any) -> Any:
            captured["input"] = input
            output = MagicMock()
            output.completion = canned_completion
            return output

        fake_model = MagicMock()
        fake_model.generate = fake_generate

        # Patch get_model where the judge module imports it.
        monkeypatch.setattr("inspect_ai.model.get_model", lambda *_a, **_kw: fake_model)

        judge = InspectAILlmJudge(model="fake/model")
        verdict = await judge.verdict(
            task=task,
            trajectory_a=_trajectory(configuration_name="a"),
            trajectory_b=_trajectory(configuration_name="b"),
        )

        assert verdict.winner == "a"
        assert verdict.confidence == 0.78
        assert verdict.structural is not None
        assert verdict.structural.task_id == "t1"

        # The prompt we sent included the structural report and both
        # trajectories — these are what the judge is reasoning over.
        prompt_text = captured["input"][1].content  # 0 = system, 1 = user
        assert task.user_prompt in prompt_text
        assert "Trajectory A" in prompt_text
        assert "Trajectory B" in prompt_text
        assert "Structural facts" in prompt_text

    def test_default_model_is_haiku(self) -> None:
        # Pinned constant — bumping the default is a deliberate change,
        # so this test guards against accidental drift.
        assert "haiku" in DEFAULT_JUDGE_MODEL

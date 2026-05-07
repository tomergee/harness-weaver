"""Unit tests for SDK message → Trajectory event translation.

These exercise :class:`SdkMessageTranslator` with synthetic SDK message
objects. No live SDK or live LLM involved — we construct the SDK's
dataclasses directly and check the events that land on the recorder.
"""

import json

import claude_agent_sdk as sdk

from harness_weaver.sdk_translate import SdkMessageTranslator
from harness_weaver.trajectory import (
    AssistantTurn,
    FinalAnswer,
    ToolResult,
    ToolUse,
    TrajectoryRecorder,
)


def _assistant(*content: object, parent_tool_use_id: str | None = None) -> sdk.AssistantMessage:
    """Build an AssistantMessage with positional content blocks."""
    return sdk.AssistantMessage(
        content=list(content),  # type: ignore[arg-type]
        model="test-model",
        parent_tool_use_id=parent_tool_use_id,
        error=None,
        usage=None,
        message_id=None,
        stop_reason=None,
        session_id=None,
        uuid=None,
    )


def _result(text: str) -> sdk.ResultMessage:
    return sdk.ResultMessage(
        subtype="success",
        duration_ms=100,
        duration_api_ms=80,
        is_error=False,
        num_turns=1,
        session_id="s1",
        stop_reason="end_turn",
        total_cost_usd=0.0,
        usage=None,
        result=text,
        structured_output=None,
        model_usage=None,
        permission_denials=None,
        deferred_tool_use=None,
        errors=None,
        api_error_status=None,
        uuid=None,
    )


def _record() -> TrajectoryRecorder:
    return TrajectoryRecorder(task_id="t", configuration_name="c")


class TestTextBlocks:
    def test_text_block_becomes_assistant_turn(self) -> None:
        rec = _record()
        SdkMessageTranslator().translate(_assistant(sdk.TextBlock(text="hello")), rec)
        traj = rec.finish()
        assert len(traj.events) == 1
        ev = traj.events[0]
        assert isinstance(ev, AssistantTurn)
        assert ev.text == "hello"
        assert ev.agent_id == "orchestrator"

    def test_empty_text_block_skipped(self) -> None:
        rec = _record()
        SdkMessageTranslator().translate(_assistant(sdk.TextBlock(text="   ")), rec)
        traj = rec.finish()
        assert len(traj.events) == 0


class TestToolUseAndResult:
    def test_tool_use_block_becomes_tool_use_event(self) -> None:
        rec = _record()
        block = sdk.ToolUseBlock(id="t1", name="search_titles", input={"genres": ["Drama"]})
        SdkMessageTranslator().translate(_assistant(block), rec)
        traj = rec.finish()
        assert len(traj.events) == 1
        ev = traj.events[0]
        assert isinstance(ev, ToolUse)
        assert ev.tool_name == "search_titles"
        assert ev.arguments == {"genres": ["Drama"]}

    def test_tool_result_with_json_payload(self) -> None:
        rec = _record()
        translator = SdkMessageTranslator()
        # First the use, then the result — translator pairs them by id.
        translator.translate(
            _assistant(sdk.ToolUseBlock(id="t1", name="get_metadata", input={"movie_id": "m1"})),
            rec,
        )
        translator.translate(
            _assistant(
                sdk.ToolResultBlock(
                    tool_use_id="t1",
                    content=[{"type": "text", "text": json.dumps({"movie": {"id": "m1"}})}],
                    is_error=False,
                )
            ),
            rec,
        )
        traj = rec.finish()
        results = [e for e in traj.events if isinstance(e, ToolResult)]
        assert len(results) == 1
        assert results[0].tool_name == "get_metadata"  # name resolved via id mapping
        assert results[0].result == {"movie": {"id": "m1"}}
        assert results[0].error is None

    def test_tool_result_with_is_error(self) -> None:
        rec = _record()
        translator = SdkMessageTranslator()
        translator.translate(
            _assistant(sdk.ToolUseBlock(id="t1", name="get_metadata", input={"movie_id": "x"})),
            rec,
        )
        translator.translate(
            _assistant(
                sdk.ToolResultBlock(
                    tool_use_id="t1",
                    content=[{"type": "text", "text": "no movie with id 'x'"}],
                    is_error=True,
                )
            ),
            rec,
        )
        results = [e for e in rec.finish().events if isinstance(e, ToolResult)]
        assert results[0].error == "no movie with id 'x'"
        assert results[0].result is None

    def test_unparsable_text_falls_back_to_raw(self) -> None:
        rec = _record()
        translator = SdkMessageTranslator()
        translator.translate(
            _assistant(sdk.ToolUseBlock(id="t1", name="custom", input={})),
            rec,
        )
        translator.translate(
            _assistant(
                sdk.ToolResultBlock(
                    tool_use_id="t1",
                    content=[{"type": "text", "text": "not json"}],
                    is_error=False,
                )
            ),
            rec,
        )
        results = [e for e in rec.finish().events if isinstance(e, ToolResult)]
        assert results[0].result == {"raw": "not json"}

    def test_unknown_tool_use_id_falls_back_to_id_label(self) -> None:
        rec = _record()
        # No matching ToolUseBlock seen first.
        SdkMessageTranslator().translate(
            _assistant(
                sdk.ToolResultBlock(
                    tool_use_id="orphan",
                    content=[{"type": "text", "text": "{}"}],
                    is_error=False,
                )
            ),
            rec,
        )
        results = [e for e in rec.finish().events if isinstance(e, ToolResult)]
        assert results[0].tool_name == "tool_use_id:orphan"


class TestSubAgentAttribution:
    def test_delegation_tool_attributes_subsequent_messages_to_worker(self) -> None:
        rec = _record()
        translator = SdkMessageTranslator()
        # Orchestrator delegates to Discovery via the Agent tool (the SDK's
        # delegation primitive in claude-agent-sdk 0.1.76).
        translator.translate(
            _assistant(
                sdk.ToolUseBlock(
                    id="task1",
                    name="Agent",
                    input={"subagent_type": "discovery", "prompt": "find candidates"},
                )
            ),
            rec,
        )
        # The worker emits a TextBlock; SDK tags it with parent_tool_use_id=task1.
        translator.translate(
            _assistant(
                sdk.TextBlock(text="searching..."),
                parent_tool_use_id="task1",
            ),
            rec,
        )
        # And calls a tool. parent_tool_use_id stays the same.
        translator.translate(
            _assistant(
                sdk.ToolUseBlock(id="t1", name="search_titles", input={}),
                parent_tool_use_id="task1",
            ),
            rec,
        )
        traj = rec.finish()
        # Skip the orchestrator's Task call; check attribution on the next two.
        worker_events = [e for e in traj.events if e.agent_id == "discovery"]
        assert len(worker_events) == 2
        worker_text = next(e for e in worker_events if isinstance(e, AssistantTurn))
        assert worker_text.text == "searching..."

    def test_unknown_parent_id_defaults_to_orchestrator(self) -> None:
        rec = _record()
        SdkMessageTranslator().translate(
            _assistant(sdk.TextBlock(text="hi"), parent_tool_use_id="never-saw-this"),
            rec,
        )
        traj = rec.finish()
        assert traj.events[0].agent_id == "orchestrator"


class TestResultMessage:
    def test_result_becomes_final_answer(self) -> None:
        rec = _record()
        SdkMessageTranslator().translate(_result("the final answer"), rec)
        traj = rec.finish()
        assert traj.final_answer == "the final answer"
        assert isinstance(traj.events[-1], FinalAnswer)

    def test_empty_result_does_not_produce_event(self) -> None:
        rec = _record()
        SdkMessageTranslator().translate(_result(""), rec)
        traj = rec.finish()
        assert traj.final_answer is None
        assert len(traj.events) == 0


class TestUserMessageToolResults:
    """UserMessage carries tool results back from the SDK — translator
    must extract them so the trajectory has tool_result events to pair
    with the tool_use events the AssistantMessage emitted."""

    def test_user_message_with_tool_result_block_recorded(self) -> None:
        rec = _record()
        translator = SdkMessageTranslator()
        # First, the assistant calls a tool.
        translator.translate(
            _assistant(sdk.ToolUseBlock(id="t1", name="search_titles", input={})),
            rec,
        )
        # The SDK echoes the result back as a UserMessage with list content.
        user_msg = sdk.UserMessage(
            content=[
                sdk.ToolResultBlock(
                    tool_use_id="t1",
                    content=[{"type": "text", "text": '{"hits": [], "total_matched": 0}'}],
                    is_error=False,
                )
            ],
            uuid=None,
            parent_tool_use_id=None,
            tool_use_result=None,
        )
        translator.translate(user_msg, rec)
        traj = rec.finish()
        results = [e for e in traj.events if isinstance(e, ToolResult)]
        assert len(results) == 1
        assert results[0].tool_name == "search_titles"
        assert results[0].result == {"hits": [], "total_matched": 0}

    def test_user_message_with_string_content_skipped(self) -> None:
        # The SDK echoes the original prompt as a UserMessage with string
        # content; the recorder already wrote a UserMessage event upfront,
        # so the translator must not double-record.
        rec = _record()
        msg = sdk.UserMessage(
            content="ping",
            uuid=None,
            parent_tool_use_id=None,
            tool_use_result=None,
        )
        SdkMessageTranslator().translate(msg, rec)
        assert len(rec.finish().events) == 0


class TestUnknownMessages:
    def test_silently_dropped(self) -> None:
        rec = _record()
        # Pass something that isn't an AssistantMessage / UserMessage / ResultMessage —
        # forward-compatibility: future SDK message types are dropped silently.
        SdkMessageTranslator().translate("not a message", rec)  # type: ignore[arg-type]
        assert len(rec.finish().events) == 0

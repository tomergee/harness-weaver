"""Translate a stream of Claude Agent SDK messages into Trajectory events.

The SDK yields heterogeneous messages on its async iterator:

    UserMessage     — input the SDK is about to send to the model.
    AssistantMessage — model output; ``.content`` is a list of typed
                       blocks (TextBlock, ToolUseBlock, ToolResultBlock,
                       ThinkingBlock, ServerToolUse/ResultBlock).
    SystemMessage   — informational banners (init, warnings, etc.).
    ResultMessage   — terminal message with cost/duration/final result.
    StreamEvent     — token-level streaming chunks.
    RateLimitEvent  — informational.

We collapse this into our :class:`Trajectory` model: AssistantTurn,
ToolUse, ToolResult, FinalAnswer. Mapping rules:

* ``TextBlock`` inside an AssistantMessage becomes an :class:`AssistantTurn`,
  except the *last* text block on the path to termination, which is kept
  as the ``final_answer`` candidate. We don't know which is "last" mid-
  stream, so every text block is recorded as ``AssistantTurn`` and then
  the recorder's ``final_answer`` is set when ``ResultMessage`` arrives.
* ``ToolUseBlock`` and ``ToolResultBlock`` map directly. Tool result
  duration is *not* available from the SDK message, so we record 0.0 and
  rely on caller-side measurement if more precision is needed.
* Sub-agent attribution: ``AssistantMessage.parent_tool_use_id`` indicates
  the message belongs to a sub-agent invocation. We translate this into
  the ``agent_id`` field on each event, defaulting to "orchestrator" when
  ``parent_tool_use_id`` is None. The mapping from ``parent_tool_use_id``
  to a worker role name is built up as we see ``Task`` ToolUse blocks
  fire — the input to a Task call carries ``subagent_type`` which is the
  worker's role name.
* ``ThinkingBlock`` is dropped — useful for debugging but not part of
  the legible-trajectory contract.
* Server-side tool blocks (``ServerToolUseBlock`` etc.) are recorded as
  ToolUse / ToolResult with ``agent_id="server"`` so they're attributable.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import claude_agent_sdk as sdk

from harness_weaver.configurations import ORCHESTRATOR_AGENT_ID
from harness_weaver.mcp_server import DEFAULT_SERVER_NAME
from harness_weaver.sdk_compile import DELEGATION_TOOL_NAME

if TYPE_CHECKING:
    from harness_weaver.trajectory import TrajectoryRecorder


class SdkMessageTranslator:
    """Stateful translator: tracks parent_tool_use_id → role_name attribution.

    The SDK doesn't tag AssistantMessages with their worker role name
    directly; instead it gives a ``parent_tool_use_id`` that points back
    to a ``Task`` ToolUseBlock whose input includes ``subagent_type``.
    The translator watches Task tool calls go by and remembers the
    ``id → subagent_type`` mapping so it can attribute downstream
    messages to the right worker.
    """

    def __init__(self, *, server_name: str = DEFAULT_SERVER_NAME) -> None:
        # The SDK exposes our MCP tools under ``mcp__<server>__<tool>``;
        # we strip that prefix so the trajectory shows ``search_titles``
        # rather than ``mcp__harness_weaver__search_titles``. Built using
        # the runner's configured server name so a custom server name
        # (matched in ``compile_options``) still strips correctly.
        self._mcp_prefix = f"mcp__{server_name}__"
        self._role_by_tool_use_id: dict[str, str] = {}
        # ToolResultBlock only carries ``tool_use_id``; we remember the name
        # from the matching ToolUseBlock so the trajectory shows it.
        self._name_by_tool_use_id: dict[str, str] = {}

    def _unqualify(self, tool_name: str) -> str:
        """Strip our MCP prefix; built-in / server-side names pass through."""
        if tool_name.startswith(self._mcp_prefix):
            return tool_name[len(self._mcp_prefix) :]
        return tool_name

    def translate(
        self,
        message: object,
        recorder: TrajectoryRecorder,
    ) -> None:
        """Append events to ``recorder`` based on ``message``."""
        if isinstance(message, sdk.AssistantMessage):
            self._translate_assistant(message, recorder)
        elif isinstance(message, sdk.UserMessage):
            self._translate_user(message, recorder)
        elif isinstance(message, sdk.ResultMessage):
            self._translate_result(message, recorder)
        # SystemMessage, StreamEvent, RateLimitEvent: drop silently. The SDK
        # may add new message types in future versions and we want to be
        # forward-compatible.

    def _translate_assistant(
        self,
        message: sdk.AssistantMessage,
        recorder: TrajectoryRecorder,
    ) -> None:
        agent_id = self._agent_id_for(message.parent_tool_use_id)
        for block in message.content:
            if isinstance(block, sdk.TextBlock):
                if block.text.strip():  # skip empty text blocks
                    recorder.assistant_turn(block.text, agent_id=agent_id)
            elif isinstance(block, sdk.ToolUseBlock):
                self._record_tool_use(block, recorder, agent_id)
            elif isinstance(block, sdk.ToolResultBlock):
                self._record_tool_result(block, recorder, agent_id)
            elif isinstance(block, sdk.ServerToolUseBlock):
                # Track the name so the matching ServerToolResultBlock can
                # resolve back to it (web_search / bash / etc.) rather than
                # falling back to a generic "server_tool" label.
                self._name_by_tool_use_id[block.id] = block.name
                recorder.tool_use(block.name, dict(block.input), agent_id="server")
            elif isinstance(block, sdk.ServerToolResultBlock):
                tool_name = self._name_by_tool_use_id.get(block.tool_use_id, "server_tool")
                recorder.tool_result(
                    tool_name,
                    result=_server_result_payload(block),
                    duration_seconds=0.0,
                    agent_id="server",
                )
            # ThinkingBlock and unknown blocks: drop.

    def _record_tool_use(
        self,
        block: sdk.ToolUseBlock,
        recorder: TrajectoryRecorder,
        agent_id: str,
    ) -> None:
        # If this is a delegation call (Agent / Task), remember which
        # worker the subsequent messages should be attributed to.
        if block.name == DELEGATION_TOOL_NAME:
            subagent_type = block.input.get("subagent_type")
            if isinstance(subagent_type, str):
                self._role_by_tool_use_id[block.id] = subagent_type
        clean_name = self._unqualify(block.name)
        self._name_by_tool_use_id[block.id] = clean_name
        recorder.tool_use(clean_name, dict(block.input), agent_id=agent_id)

    def _record_tool_result(
        self,
        block: sdk.ToolResultBlock,
        recorder: TrajectoryRecorder,
        agent_id: str,
    ) -> None:
        result, error = _parse_tool_result(block)
        # Fall back to the use-id when we never saw the matching ToolUseBlock
        # (e.g. translator constructed mid-conversation). Keeps the field
        # populated for trajectory consumers.
        tool_name = self._name_by_tool_use_id.get(
            block.tool_use_id, f"tool_use_id:{block.tool_use_id}"
        )
        recorder.tool_result(
            tool_name=tool_name,
            result=result,
            error=error,
            duration_seconds=0.0,
            agent_id=agent_id,
        )

    def _translate_user(
        self,
        message: sdk.UserMessage,
        recorder: TrajectoryRecorder,
    ) -> None:
        """UserMessage carries tool results back to the assistant.

        The SDK echoes our user prompt (with ``content: str``) and also
        emits UserMessages whose ``content`` is a list including
        ``ToolResultBlock`` — that's how tool execution results re-enter
        the conversation. We only record the result blocks; the prompt
        echo was already recorded by the harness itself.
        """
        if isinstance(message.content, str):
            return  # the user prompt echo; nothing to record
        agent_id = self._agent_id_for(message.parent_tool_use_id)
        for block in message.content:
            if isinstance(block, sdk.ToolResultBlock):
                self._record_tool_result(block, recorder, agent_id)
            # Other block types in UserMessage.content are echoed assistant
            # output, ignored to avoid double-recording.

    def _translate_result(
        self,
        message: sdk.ResultMessage,
        recorder: TrajectoryRecorder,
    ) -> None:
        # ResultMessage.result holds the final assistant text.
        if message.result:
            recorder.final_answer(message.result)

    def _agent_id_for(self, parent_tool_use_id: str | None) -> str:
        if parent_tool_use_id is None:
            return ORCHESTRATOR_AGENT_ID
        return self._role_by_tool_use_id.get(parent_tool_use_id, ORCHESTRATOR_AGENT_ID)


def _parse_tool_result(
    block: sdk.ToolResultBlock,
) -> tuple[dict[str, Any] | None, str | None]:
    """Pull the JSON payload (or error string) out of an MCP ToolResultBlock.

    Our MCP server always returns ``{"content": [{"type": "text", "text": <json>}]}``,
    so the happy-path content[0].text is JSON we wrote. On the error path
    (``is_error=True``), the text is a plain error string from ``ToolError``.
    Tools we did not write — Task, builtin tools when enabled — may return
    arbitrary shapes; fall back to recording the text verbatim under a
    ``raw`` key so nothing is lost.
    """
    if block.is_error:
        return None, _extract_text(block.content)

    text = _extract_text(block.content)
    if text is None:
        return None, None

    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {"raw": text}, None

    if isinstance(parsed, dict):
        return parsed, None
    # Tool returned a non-object JSON value (rare); wrap so the trajectory
    # schema's ``result: dict | None`` invariant holds.
    return {"value": parsed}, None


def _extract_text(content: str | list[dict[str, Any]] | None) -> str | None:
    """Pull text out of an MCP content payload (str, list of blocks, or None)."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    # list[dict]: typically one block of {"type": "text", "text": "..."}.
    parts: list[str] = []
    for entry in content:
        if isinstance(entry, dict) and entry.get("type") == "text":
            text = entry.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts) if parts else None


def _server_result_payload(block: sdk.ServerToolResultBlock) -> dict[str, Any]:
    """Render a server-side tool result block as a payload dict.

    Server tool results carry a free-form ``content`` dict; we pass it
    through under a ``content`` key alongside the use id so the original
    structure stays inspectable in the trajectory.
    """
    return {"tool_use_id": block.tool_use_id, "content": dict(block.content)}


__all__ = ["SdkMessageTranslator"]

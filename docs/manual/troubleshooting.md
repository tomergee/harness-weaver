# Troubleshooting

The gotchas in this list are real failures we hit during the live SDK
integration, with the cause and the fix beside each. If you see one of
these symptoms, search this file before debugging from scratch.

## Live SDK auth and permissions

### "`--dangerously-skip-permissions` cannot be used with root/sudo privileges"

**Symptom:** The first live `harness-weaver run` fails immediately with
this message in stderr.

**Cause:** The underlying Claude Code CLI refuses the
`bypassPermissions` mode when running as root, as a safety check. The
SDK's permission_mode `"bypassPermissions"` translates to that flag.

**Fix:** Already applied — `compile_options` uses
`permission_mode="default"`, which auto-permits anything in
`allowed_tools` without bypassing the safety guard. If you change the
mode in your fork, keep it off `"bypassPermissions"` when running as
root.

### Every tool call returns "Claude requested permissions to use mcp__harness_weaver__... but you haven't granted it yet"

**Symptom:** The trajectory has `tool_use` events but every paired
`tool_result` has `error` set with the message above. The agent gives
up and apologizes.

**Cause:** The SDK exposes MCP tools to the model under the name
`mcp__<server>__<tool>` but `ClaudeAgentOptions.allowed_tools` was
populated with bare names. With `permission_mode="default"`, anything
not literally on the allow-list is treated as unpermitted.

**Fix:** Already applied —
[`harness_weaver.mcp_server.qualified_tool_name`](../../src/harness_weaver/mcp_server.py)
prefixes the names, and `compile_options` calls it for both the
top-level `allowed_tools` and the per-worker `AgentDefinition.tools`.
The translator strips the prefix back out for trajectory readability.

### Multi-agent: orchestrator never delegates

**Symptom:** With `multi-agent-discovery-explainer`, the trajectory
shows the orchestrator calling catalog tools directly — no `Agent`
delegation events, no events tagged with worker `agent_id`s. Workers
look like they don't exist.

**Cause:** `ClaudeAgentOptions.tools=[]` disables every built-in tool,
including the SDK's `Agent` delegation tool (named `Task` in older
Claude Code versions). Without `Agent`, the orchestrator silently
falls back to calling the catalog tools itself with permission errors.

**Fix:** Already applied — `compile_options` keeps `Agent` in `tools`
and `allowed_tools` whenever `Configuration.is_multi_agent` is true.
If you write your own multi-agent configuration, no extra wiring is
needed; the compiler does it.

### Final answer mentions uncommitted git changes / project files / settings

**Symptom:** The agent's final answer veers into discussing the
*harness's own repository state* — git status, settings files, etc. —
instead of the user's task.

**Cause:** Without explicit `setting_sources=[]` and `skills=[]`,
Claude Code injects the host's project / user / local settings into
the system prompt. The model is honest about seeing them and gets
distracted.

**Fix:** Already applied — `compile_options` sets both fields to empty
lists. If you customize `compile_options` or build options manually,
keep these two suppressions or expect the same drift.

## Tooling

### `make check` fails locally but CI passes (or vice versa)

**Cause:** `make check` runs `ruff format` (mutating), then `ruff check`
and `mypy` and `pytest`. CI runs `ruff format --check` (non-mutating).
Locally-formatted-but-not-formatter-clean code can pass `make check`
locally and fail CI's `--check` step on the very same files.

**Fix:** Run `make fmt` before pushing, or use a pre-commit hook
(`make install` sets one up).

### `pytest` fails with "Required test coverage of 70% not reached"

**Cause:** You ran `pytest <a_single_file>`. Coverage is measured over
the whole suite by default; a single file doesn't exercise enough of
`src/`.

**Fix:** Either run the full suite (`pytest` with no args), or override
the gate locally: `pytest tests/test_tools.py --cov-fail-under=0`.

## Catalog and data

### `harness-weaver run` fails with "no history for user 'user-XXX'"

**Cause:** The Task references a `user_id` that isn't in
`src/harness_weaver/data/ratings.csv`. Eight users ship — see
`Catalog.known_users`.

**Fix:** Either edit the Task to use a known user, or add ratings to
`ratings.csv` for the new user. The catalog validates at construction
that every rating points to a real movie, so add the user's events
referencing existing `m###` ids.

### Search returns fewer hits than expected

The bundled catalog is small — 60 titles. A query like
`{"genres": ["Documentary"]}` returns nothing because no documentaries
ship in the dataset. Check
`src/harness_weaver/data/catalog.csv` if you're getting empty results
on a plausible query.

## Trajectories

### `Trajectory.tool_call_count` doesn't match the number of `tool_result` events

**Possible causes:**

1. The agent emitted a `ToolUse` and the SDK rejected it before
   execution — the result block carries `is_error=True` and our
   translator records it as a `ToolResult` with `error` set.
2. The agent died mid-turn (rate limit, model error). Trajectories
   from interrupted runs are still recorded but may have a `ToolUse`
   without a matching `ToolResult`.

**Diagnosis:** Iterate the trajectory and look for `ToolResult` events
whose `error` is non-`None`, or `ToolUse` events without a paired
result. Both are legitimate states; you may want to filter them out
when computing aggregate statistics.

### `final_answer` is `None`

**Cause:** The run never produced a `FinalAnswer` event. Usually this
means the SDK's `ResultMessage` arrived without a `result` field —
either the agent hit max turns, or an error message terminated the
session.

**Diagnosis:** Look at the last few events. If there's an
`AssistantTurn` with text that reads like a final answer, the model
just didn't formally terminate. If the events stop abruptly, check
stderr for SDK errors.

## Tests

### A test referencing live SDK message types fails after upgrading `claude-agent-sdk`

**Cause:** SDK message types are dataclasses; their fields can change
between releases. Tests that construct `AssistantMessage(...)` etc.
directly (in `tests/test_sdk_translate.py`,
`tests/test_real_agent_runner.py`) need the same field set the SDK
exports.

**Fix:** Compare your installed SDK's `dataclasses.fields(<MessageType>)`
to the kwargs the test passes. The diff is usually 1-2 fields and
mechanical.

## Filing a new gotcha

If you hit something not on this list, edit this file. Pattern:

```markdown
### Symptom (one-line, as a user would describe it)

**Cause:** Why it happens. Cite line numbers if useful.

**Fix:** What to change. If it's already applied, link the file.
```

That's the playbook. Back to the [manual index](README.md).

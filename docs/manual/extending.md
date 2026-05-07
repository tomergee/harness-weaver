# Extending the harness

Four common extensions, in roughly increasing complexity:

1. [Add a new Task](#add-a-new-task) (~5 minutes, no Python)
2. [Add a new Configuration](#add-a-new-configuration) (~15 minutes)
3. [Add a new Tool](#add-a-new-tool) (~30 minutes)
4. [Swap the ExecutionBackend](#swap-the-executionbackend) (~30 minutes)

## Add a new Task

A Task is a JSON file. Drop one in `examples/tasks/`:

```json
{
  "task_id": "discovery-cozy-mystery",
  "user_prompt": "I'm in the mood for something gentle but with a puzzle. Under 2 hours.",
  "user_id": "user-006",
  "expected_outcome": "Recommends one or more cozy mystery / light thriller titles with runtime < 120 minutes.",
  "success_criteria": {
    "min_results": 1,
    "max_runtime_minutes": 120
  },
  "tags": ["discovery", "mood-based", "cozy"]
}
```

Fields are validated against `Task` (extra keys rejected). See
[`src/harness_weaver/task.py`](../../src/harness_weaver/task.py) for the
schema. Then:

```bash
harness-weaver run examples/tasks/discovery-cozy-mystery.json -c single-agent-basic
```

Pack-level evaluation: wrap several tasks in a `TaskPack`:

```json
{
  "name": "discovery-pack",
  "description": "Mood-based discovery prompts.",
  "tasks": [
    { "task_id": "discovery-mood-tense", "user_prompt": "..." },
    { "task_id": "discovery-cozy-mystery", "user_prompt": "..." }
  ]
}
```

Then `harness-weaver eval examples/packs/discovery-pack.json`.

## Add a new Configuration

The clean path is in code. Edit
[`src/harness_weaver/configurations.py`](../../src/harness_weaver/configurations.py)
and append:

```python
SINGLE_AGENT_TERSE = Configuration(
    name="single-agent-terse",
    description=(
        "Single agent told to be brief and skip explanations. Used to compare "
        "whether terseness hurts grounding."
    ),
    system_prompt=(
        "You are a film recommendation assistant. Reply in at most three sentences. "
        "Use the catalog tools but do not narrate the search; only justify the final "
        "recommendation."
    ),
    allowed_tools=("search_titles", "get_metadata", "user_history"),
)

# ... and add it to _BUILTIN:
_BUILTIN: dict[str, Configuration] = {
    c.name: c
    for c in (
        SINGLE_AGENT_BASIC,
        SINGLE_AGENT_WITH_SANDBOX,
        SINGLE_AGENT_TERSE,                      # new
        MULTI_AGENT_DISCOVERY_EXPLAINER,
    )
}
```

The new configuration shows up in `harness-weaver list-configs`
automatically.

### Multi-agent

Add workers via `agents`:

```python
TRIO_DEBATE = Configuration(
    name="trio-debate",
    description="Three workers argue, orchestrator picks a winner.",
    system_prompt="You are an orchestrator. Ask three workers, then pick one.",
    allowed_tools=(),
    agents=(
        AgentDefinition(role_name="optimist", system_prompt="...", allowed_tools=("get_metadata",)),
        AgentDefinition(role_name="skeptic", system_prompt="...", allowed_tools=("get_metadata",)),
        AgentDefinition(role_name="historian", system_prompt="...", allowed_tools=("search_titles", "get_metadata")),
    ),
)
```

Validation rules:

* `role_name` is required and must be unique within the configuration.
* `role_name` cannot be `"orchestrator"` (reserved for the top-level
  agent). Both rules are enforced by a pydantic `model_validator`; see
  ADR-0002 for why.

### From JSON, ad-hoc

`Configuration.from_path(path)` loads one off disk. Useful for
experiments where you don't want to touch the source tree:

```python
from pathlib import Path
from harness_weaver.configurations import Configuration

cfg = Configuration.from_path(Path("my-experiment.json"))
```

## Add a new Tool

Tools live under
[`src/harness_weaver/tools/`](../../src/harness_weaver/tools/). Three
files to touch:

1. **Define input/output models and the tool class.** Subclass `Tool`,
   set `name`, `description`, `input_model`, `output_model`, and
   implement `execute`.
2. **Register it.** Add it to the `if "<your_tool_name>" in wanted:`
   block in
   [`Harness._build_registry`](../../src/harness_weaver/harness.py).
3. **Whitelist it.** Add `"<your_tool_name>"` to the `allowed_tools` of
   any Configuration that should expose it.

Example: a `top_genre_for_user` tool that summarizes a user's favored genres.

```python
# src/harness_weaver/tools/profile_tools.py
from collections import Counter
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from harness_weaver.catalog import Catalog
from harness_weaver.tools.base import Tool, ToolError


class TopGenreInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(description="Stable user id, e.g. 'user-001'.")
    top_n: int = Field(default=3, ge=1, le=10)


class TopGenreOutput(BaseModel):
    user_id: str
    top_genres: list[tuple[str, int]]


class TopGenreTool(Tool[TopGenreInput, TopGenreOutput]):
    name: ClassVar[str] = "top_genre_for_user"
    description: ClassVar[str] = (
        "Return the top-N most-watched genres for a user, ranked by count, "
        "based on their rating history."
    )
    input_model = TopGenreInput
    output_model = TopGenreOutput

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    def execute(self, args: TopGenreInput) -> TopGenreOutput:
        events = self._catalog.history_for(args.user_id)
        if not events and args.user_id not in self._catalog.known_users:
            raise ToolError(f"no history for user {args.user_id!r}")
        counter: Counter[str] = Counter()
        for ev in events:
            movie = self._catalog.get(ev.movie_id)
            assert movie is not None
            counter.update(movie.genres)
        return TopGenreOutput(
            user_id=args.user_id,
            top_genres=counter.most_common(args.top_n),
        )
```

Then in `Harness._build_registry`:

```python
from harness_weaver.tools.profile_tools import TopGenreTool

if "top_genre_for_user" in wanted:
    registry.register(TopGenreTool(self._catalog))
```

And in any Configuration that should see it:

```python
allowed_tools=("search_titles", "get_metadata", "user_history", "top_genre_for_user"),
```

Tests for the new tool sit alongside the existing ones in
[`tests/test_tools.py`](../../tests/test_tools.py); the patterns there
(dispatch via `tool.call(dict)`, schema assertions, ToolError cases)
transfer directly.

### What the Tool boundary buys you

* **Schema for free**: `tool.input_schema()` returns the JSON Schema
  pydantic generates from your `input_model`. The MCP server wrapper
  uses this directly, so the model sees a well-typed tool surface.
* **Validation at the boundary**: `tool.call({...})` validates the dict
  against `input_model` and raises `ToolError` on bad input. The agent
  sees a structured error rather than a Python traceback.
* **Transport agnosticism**: tools don't know whether they're being
  dispatched in-process (via `create_sdk_mcp_server`) or over stdio.
  See ADR-0004.

## Swap the ExecutionBackend

`run_python` runs through whatever `ExecutionBackend` is injected into
the Harness. The default `LocalSubprocessBackend` is dev-only; for real
isolation, write a backend that wraps Kubernetes / Firecracker / etc.

```python
class MyBackend(ExecutionBackend):
    def run(self, request: ExecutionRequest) -> ExecutionResult:
        # ... whatever isolation your environment needs ...
        return ExecutionResult(
            exit_code=0,
            stdout="...",
            stderr="",
            timed_out=False,
            duration_seconds=0.42,
        )
```

Pass it to the Harness:

```python
Harness(
    catalog=Catalog.load_default(),
    runner=RealAgentRunner(),
    execution_backend=MyBackend(),
)
```

Contract:

* `run` may not raise on non-zero exit, captured stderr, or timeout —
  those are normal results. Raise only on backend-infrastructure
  failures (broken pipe, missing interpreter, etc.).
* `timed_out=True` means the backend killed the process at the
  deadline; `exit_code` is undefined in that case.
* `duration_seconds` is wall-clock, not CPU time.

ADR-0003 (sandbox lifecycle) will document the warm-vs-fresh decision
once the K8s backend lands.

Next: [Troubleshooting](troubleshooting.md).

# Concepts

Eight types do most of the work in `harness-weaver`. Knowing what each
one is for — and *isn't* for — is most of how to use the library.

```text
                  ┌───────────────────┐
                  │      Harness      │   single entry point
                  │  .run(task, cfg)  │
                  └────────┬──────────┘
                           │ builds a registry, composes a prompt
                           ▼
   Configuration ───┬──> ToolRegistry <── Tool, Tool, Tool ...
                    │       │
   Task ────────────┘       │ (in-process MCP)
                            ▼
                       AgentRunner ──> Trajectory
                       (Real or Fake)   (audit log)
```

## Task

A `Task` is the prompt plus optional context. JSON-loadable, frozen,
`extra="forbid"`. See [`src/harness_weaver/task.py`](../../src/harness_weaver/task.py).

```python
class Task(BaseModel):
    task_id: str
    user_prompt: str
    user_id: str | None = None
    expected_outcome: str | None = None       # read by the judge later
    success_criteria: dict[str, Any] = {}     # structured success checks
    tags: list[str] = []
```

Bundled examples live in [`examples/tasks/`](../../examples/tasks/).
Tasks are deliberately data, not code — you can ship them, version
them, hand them to a non-engineer.

## Configuration

A `Configuration` is the **unit of variation**. Two runs of the same
Task on different Configurations differ in exactly the thing the
Configuration says they should:

```python
class Configuration(BaseModel):
    name: str
    description: str
    system_prompt: str
    allowed_tools: tuple[str, ...]            # orchestrator's tool surface
    agents: tuple[AgentDefinition, ...] = ()  # workers, for multi-agent
    model: str | None = None                  # pin a specific model id
```

Three built-ins ship with the package:

| Configuration | Topology | Tools |
|---|---|---|
| `single-agent-basic` | single agent | `search_titles`, `get_metadata`, `user_history` |
| `single-agent-with-sandbox` | single agent | + `run_python` |
| `multi-agent-discovery-explainer` | orchestrator + 2 workers | scoped per worker |

Workers (`AgentDefinition`) carry their own `system_prompt` and
`allowed_tools`. Two architectural rules apply:

* **Reserved name**: a worker cannot be called `"orchestrator"` —
  validation rejects it. ADR-0002 codifies why.
* **Subset semantics**: a worker's `allowed_tools` is a subset of what
  the harness registers; the runner enforces the per-agent allow-list
  even when the underlying SDK doesn't.

## Trajectory

A `Trajectory` is the auditable record of one `Harness.run()`. Five
event kinds, discriminated by `type`:

| Event | When |
|---|---|
| `UserMessage` | The prompt the harness handed to the agent. Always one, at the start. |
| `AssistantTurn` | Free-form text the agent emitted between tool calls. |
| `ToolUse` | The agent invoked a tool. Includes name + arguments. |
| `ToolResult` | The tool returned (or errored). Has `result` *or* `error`, never both. |
| `FinalAnswer` | The agent's terminal response. |

Every event has an `agent_id` so multi-agent runs are attributable
(`"orchestrator"`, `"discovery"`, `"explainer"`, ...).

The Trajectory itself carries two run-level fields beyond the event
list, populated when the SDK reports them on its terminal
`ResultMessage`:

* `total_cost_usd: float | None` — provider cost in USD. `None` for
  trajectories from `FakeAgentRunner` (no model call) and for older
  SDK paths that don't surface cost.
* `num_turns: int | None` — number of model turns the SDK counted.

The judge layer's pack rollup sums `total_cost_usd` across runs (and
flags partial coverage when only some trajectories tracked cost — see
`judge.md`).

Trajectories round-trip through JSON; pydantic v2 handles the
discriminated union. Inspecting one is just `json.load` + iterate.

## Tool

A `Tool` is a typed, MCP-shaped operation:

```python
class Tool(ABC, Generic[InputT, OutputT]):
    name: ClassVar[str]
    description: ClassVar[str]
    input_model: type[InputT]      # pydantic model, doubles as JSON schema
    output_model: type[OutputT]    # pydantic model

    def execute(self, args: InputT) -> OutputT: ...     # the actual work
    def call(self, arguments: dict) -> dict: ...        # MCP-shaped boundary
    def input_schema(self) -> dict: ...                 # for tool listing
```

Three pure-data tools (`SearchTitlesTool`, `GetMetadataTool`,
`UserHistoryTool`) plus one dangerous tool (`RunPythonTool`) ship in
[`src/harness_weaver/tools/`](../../src/harness_weaver/tools/).

## ExecutionBackend

The seam where dangerous tools (`run_python`) actually execute:

```python
class ExecutionBackend(ABC):
    def run(self, request: ExecutionRequest) -> ExecutionResult: ...
```

`LocalSubprocessBackend` is the dev backend — runs Python in a child
process with env scrub, fresh temp-dir cwd, wall-clock timeout. No real
isolation; that's K8s territory (`AgentSandboxBackend`, future work).

`RunPythonTool` doesn't know which backend it has — that decoupling is
deliberate. Above the seam, tools are dispatchable; below, the
implementation can swap from `subprocess.run` to a Kubernetes pod.

## AgentRunner

The strategy for actually running an agent session:

```python
class AgentRunner(ABC):
    def run(self, *, prompt, configuration, registry, task_id) -> Trajectory: ...
```

Two implementations:

* **`RealAgentRunner`** — drives `claude_agent_sdk.query()` against an
  in-process MCP server (see `mcp_server.py` and ADR-0004). Live model
  in the loop. Needs `ANTHROPIC_API_KEY`. Accepts a `query_fn` argument
  so tests can inject a scripted fake without an API key.
* **`FakeAgentRunner`** — replays a scripted sequence (`say` / `call` /
  `answer`) but invokes the **real** tool registry. The full pipeline
  is exercised in tests; only the LLM is stubbed.

The seam matters: the same `Harness` runs both. Swap the runner, swap
nothing else.

## Harness

One method, three steps:

```python
class Harness:
    def __init__(self, *, catalog: Catalog, runner: AgentRunner,
                 execution_backend: ExecutionBackend | None = None): ...

    def run(self, task: Task, configuration: Configuration) -> Trajectory:
        registry = self._build_registry(configuration)   # union of tool surfaces
        prompt   = self._compose_prompt(task)            # appends [user_id=...]
        return self._runner.run(
            prompt=prompt, configuration=configuration,
            registry=registry, task_id=task.task_id,
        )
```

The registry contains every tool any agent in the configuration might
need (orchestrator + workers, deduped). Per-agent allow-list
enforcement happens inside the runner when it observes a tool call.

That's the whole vocabulary. Next: [CLI reference](cli.md).

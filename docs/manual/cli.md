# CLI reference

`harness-weaver` is the entry point. Three commands run agents, one
introspects.

```text
$ harness-weaver --help
Usage: harness-weaver [OPTIONS] COMMAND [ARGS]...

  Experimentation harness for agentic systems on recommendation-style tasks.

Options:
  -V, --version  Print version and exit.
  --help         Show this message and exit.

Commands:
  compare       Run the same task under two configurations and emit ...
  eval          Evaluate one configuration against a full task pack.
  list-configs  List built-in configuration names.
  run           Run a single task with one configuration; emit a trajectory.
```

All commands need credentials only when they actually call the model.
`list-configs` works offline.

## `harness-weaver run`

Run one Task under one Configuration; write the resulting Trajectory.

```bash
harness-weaver run TASK [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `TASK` | (positional, required) | Path to a JSON Task. Must exist. |
| `--config`, `-c` | `single-agent-basic` | Built-in configuration name. |
| `--model` | `None` (SDK default) | Override the configuration's pinned model id, e.g. `claude-haiku-4-5-20251001`. |
| `--output-dir` | `runs/` | Directory for the written Trajectory. Created if missing. |

Output filename: `{task_id}.{configuration_name}.json`.

### Examples

```bash
# Default config + model:
harness-weaver run examples/tasks/discovery-mood-tense.json

# Pin Haiku:
harness-weaver run examples/tasks/discovery-mood-tense.json \
    -c single-agent-basic --model claude-haiku-4-5-20251001

# Multi-agent topology:
harness-weaver run examples/tasks/discovery-mood-tense.json \
    -c multi-agent-discovery-explainer --model claude-haiku-4-5-20251001
```

## `harness-weaver compare`

Run the same Task under two Configurations; write both trajectories.

```bash
harness-weaver compare TASK --config-a A --config-b B [OPTIONS]
```

| Option | Required | Description |
|---|---|---|
| `TASK` | yes | Same as `run`. |
| `--config-a` | yes | First configuration name. |
| `--config-b` | yes | Second configuration name. |
| `--model` | no | Override the model for **both** runs (so the only thing varying between them is the configuration). |
| `--output-dir` | no | Default `runs/`. |

The judge step (LLM-as-judge over the two trajectories) is not yet
implemented. For now this command writes both trajectories so a future
judge can consume them without re-running the agent. See the project
README's "What's missing on purpose" for the rationale.

### Example

```bash
harness-weaver compare examples/tasks/analytical-runtime-rating.json \
    --config-a single-agent-basic \
    --config-b single-agent-with-sandbox \
    --model claude-haiku-4-5-20251001
```

Inspect both outputs in `runs/`:

```text
runs/analytical-runtime-rating.single-agent-basic.json
runs/analytical-runtime-rating.single-agent-with-sandbox.json
```

## `harness-weaver eval`

Run a Configuration over a whole Task pack.

```bash
harness-weaver eval PACK [OPTIONS]
```

| Option | Default | Description |
|---|---|---|
| `PACK` | (positional) | Path to a JSON `TaskPack`. |
| `--config`, `-c` | `single-agent-basic` | Configuration name. |
| `--model` | `None` | Override pinned model. |
| `--output-dir` | `runs/` | Per-task trajectories land here. |

Per-task trajectories are written to `output_dir`. The aggregate judge
report lands with the judge integration.

A TaskPack is just `{name, description, tasks: [Task, Task, ...]}`. See
[`src/harness_weaver/task.py`](../../src/harness_weaver/task.py).

## `harness-weaver list-configs`

Print every built-in Configuration with its description. Offline, no
key required. Useful for discovery and CI sanity checks.

```text
$ harness-weaver list-configs
single-agent-basic: Single agent with the catalog tools (search_titles,
get_metadata, user_history). The baseline against which every other
configuration is compared.
single-agent-with-sandbox: Same as single-agent-basic, plus run_python
for sandboxed code execution. Use this when comparing whether code
execution improves analytical queries.
multi-agent-discovery-explainer: Orchestrator delegates discovery to a
Discovery worker (full catalog access) and presentation to an Explainer
worker (metadata only). Tests whether splitting the work across
specialist workers improves recommendation quality.
```

## Exit behavior

| Exit code | Meaning |
|---|---|
| `0` | Trajectory written. |
| non-zero | An exception bubbled up. The traceback is in stderr; common cases are unknown configuration name (`KeyError`), bad task JSON (`pydantic.ValidationError`), or SDK auth failure on a live run. |

The CLI does **not** swallow exceptions — that's intentional. Programmatic
callers should drive `Harness.run()` directly rather than parsing CLI
exit codes.

Next: [Extending the harness](extending.md).

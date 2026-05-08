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

Run the same Task under two Configurations; write both trajectories
plus a side-by-side structural report. Optionally invoke the LLM
judge for a paid quality verdict.

```bash
harness-weaver compare TASK --config-a A --config-b B [OPTIONS]
```

| Option | Required | Description |
|---|---|---|
| `TASK` | yes | Same as `run`. |
| `--config-a` | yes | First configuration name. |
| `--config-b` | yes | Second configuration name. |
| `--model` | no | Override the model for **both** runs (so the only thing varying between them is the configuration). |
| `--judge-model` | no | Run the LLM-as-judge with this Inspect-AI model id (e.g. `anthropic/claude-haiku-4-5-20251001`). When set, writes a JSON verdict alongside the markdown report. Without this flag, only the rules-based structural report is produced — no API call. |
| `--output-dir` | no | Default `runs/`. |

**Two layers of judgment**, both rooted in the same trajectories:

* **Structural report** — rules-based, deterministic, free. Always
  produced. Counts events, classifies failure modes, pass/fails any
  `Task.success_criteria`. Lands as `{task_id}.compare.md`.
* **LLM-as-judge verdict** — opt-in via `--judge-model`. Sends both
  trajectories plus the structural report to Claude (via inspect-ai)
  and emits a JSON verdict (winner, reasoning, confidence). Lands as
  `{task_id}.compare.verdict.json`. Costs money.

See [Judging trajectories](judge.md) for the rubric and prompt.

Output filenames:

* `{task_id}.{config_a_name}.json` — trajectory A
* `{task_id}.{config_b_name}.json` — trajectory B
* `{task_id}.compare.md` — structural report (always)
* `{task_id}.compare.verdict.json` — LLM verdict (when `--judge-model` set)

### Examples

```bash
# Structural report only — free, no API call:
harness-weaver compare examples/tasks/analytical-runtime-rating.json \
    --config-a single-agent-basic \
    --config-b single-agent-with-sandbox \
    --model claude-haiku-4-5-20251001

# Plus LLM-as-judge verdict (~$0.005 per verdict on Haiku):
harness-weaver compare examples/tasks/analytical-runtime-rating.json \
    --config-a single-agent-basic \
    --config-b single-agent-with-sandbox \
    --model claude-haiku-4-5-20251001 \
    --judge-model anthropic/claude-haiku-4-5-20251001
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

Output:
* `{task_id}.{config_name}.json` — one trajectory per task in the pack.
* `{pack_name}.{config_name}.eval.md` — aggregate markdown report
  covering completion rate, failure-mode frequencies, success-criteria
  pass rates, tool-call statistics, total duration, and total cost (when
  the SDK reported it).

A TaskPack is just `{name, description, tasks: [Task, Task, ...]}`. See
[`src/harness_weaver/task.py`](../../src/harness_weaver/task.py).
[`examples/packs/discovery.json`](../../examples/packs/discovery.json)
is the bundled example.

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

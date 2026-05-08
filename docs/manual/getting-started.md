# Getting started

## Requirements

* **Python 3.11+** (3.11 and 3.12 are tested in CI).
* No Kubernetes needed for the default path. The K8s execution backend
  is a future-work item.
* For live model runs: an `ANTHROPIC_API_KEY`. For everything else
  (running the test suite, exercising the harness with a scripted
  fake), no key is required.

## Install

```bash
git clone https://github.com/tomergee/harness-weaver
cd harness-weaver
pip install -e ".[dev]"     # `make install` does the same plus pre-commit hooks
make check                  # ruff format + lint, mypy --strict, pytest
```

A clean install ends with `245 passed` and the coverage gate at ≥70%
(actual is around 93%). If something fails here, fix it before going
further — every other section assumes the gate is green.

## Your first run, without an API key

Drive the `Harness` directly with a `FakeAgentRunner`. The script you
hand it captures the *agent's* decisions; the *tools* run for real
against the bundled catalog.

```python
from harness_weaver.agent_runner import FakeAgentRunner, say, call, answer
from harness_weaver.catalog import Catalog
from harness_weaver.configurations import SINGLE_AGENT_BASIC
from harness_weaver.harness import Harness
from harness_weaver.task import Task

task = Task.from_path("examples/tasks/discovery-mood-tense.json")
runner = FakeAgentRunner([
    say("Looking up the user's history first."),
    call("user_history", {"user_id": "user-001", "limit": 10}),
    say("Now searching for thrillers under 120 minutes."),
    call("search_titles", {"genres": ["Thriller"], "max_runtime": 120}),
    answer("I recommend Get Out — 104 min, modern thriller, not in your history."),
])
trajectory = Harness(catalog=Catalog.load_default(), runner=runner).run(
    task, SINGLE_AGENT_BASIC,
)
print(trajectory.model_dump_json(indent=2))
```

This exercises the full path: registry construction from the
configuration, prompt composition, allow-list enforcement, the
trajectory recorder, and JSON round-trip. Only the LLM is stubbed.

## Your first run, with an API key

```bash
# .env in the repo root is gitignored. Put your key there or export it.
export ANTHROPIC_API_KEY=sk-ant-...

harness-weaver run examples/tasks/discovery-mood-tense.json \
    --config single-agent-basic \
    --model claude-haiku-4-5-20251001
```

What you should see:

```text
trajectory written to runs/discovery-mood-tense.single-agent-basic.json
```

Inspect the trajectory:

```bash
python -m json.tool runs/discovery-mood-tense.single-agent-basic.json | head -40
```

Each event is a discriminated-union pydantic record. The trajectory
also carries provider-reported cost and turn count when the SDK
surfaces them (live runs); fake-runner trajectories leave both fields
as `null`:

```json
{
  "task_id": "discovery-mood-tense",
  "configuration_name": "single-agent-basic",
  "started_at": "2026-05-07T16:48:23Z",
  "completed_at": "2026-05-07T16:48:31Z",
  "events": [
    { "type": "user_message", "content": "Find me a tense thriller ..." },
    { "type": "tool_use", "tool_name": "user_history", "arguments": {...} },
    { "type": "tool_result", "tool_name": "user_history", "result": {...} },
    ...
    { "type": "final_answer", "text": "I'd recommend Ex Machina ..." }
  ],
  "final_answer": "I'd recommend Ex Machina ...",
  "total_cost_usd": 0.018768,
  "num_turns": 5
}
```

The committed examples in [`examples/output/`](../../examples/output/)
are real Haiku runs of all three configurations.

## Verify everything

```bash
make check                              # the gate
harness-weaver list-configs             # see the three built-in configurations
```

`list-configs` prints:

```text
single-agent-basic: Single agent with the catalog tools (search_titles,
get_metadata, user_history). The baseline against which every other
configuration is compared.
single-agent-with-sandbox: Same as single-agent-basic, plus run_python ...
multi-agent-discovery-explainer: Orchestrator delegates discovery ...
```

If `list-configs` works and `make check` is green, you're ready to
move on to [Concepts](concepts.md).

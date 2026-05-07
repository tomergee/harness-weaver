# Judging trajectories

`harness-weaver compare` produces two layers of judgment, used together
to answer "which configuration ran better on this task":

1. **Structural report** — rules-based, deterministic, free. Counts
   events, tool calls, tool errors. Tags failure modes
   (`hallucinated_tool`, `infinite_loop`, `off_task`, `refusal`,
   `cost_blowup`). Pass/fails any `Task.success_criteria`. Always runs.
2. **LLM-as-judge verdict** — opt-in via `--judge-model`. Sends both
   trajectories plus the structural report to Claude (via inspect-ai)
   and asks for a JSON verdict: winner, reasoning, confidence. Costs
   money.

The structural report is fed *into* the LLM judge's prompt as scaffolding
so the model gets the counts and reasons about them rather than
re-counting from raw events. ADR coverage will follow if the prompt
shape becomes contentious.

## Quick run

```bash
export ANTHROPIC_API_KEY=sk-ant-...

harness-weaver compare examples/tasks/discovery-mood-tense.json \
    --config-a single-agent-basic \
    --config-b multi-agent-discovery-explainer \
    --model claude-haiku-4-5-20251001 \
    --judge-model anthropic/claude-haiku-4-5-20251001
```

Output (in `runs/` by default):

```text
runs/discovery-mood-tense.single-agent-basic.json              # trajectory A
runs/discovery-mood-tense.multi-agent-discovery-explainer.json # trajectory B
runs/discovery-mood-tense.compare.md                           # structural report
runs/discovery-mood-tense.compare.verdict.json                 # LLM verdict
```

Without `--judge-model`, only the trajectories and the structural
report are written — no API call made. That's the right default for
iterating on configurations: run cheap, judge selectively.

## Failure modes

Trajectories are tagged with the modes they exhibit. Empty list = the
run looks clean by structural criteria (the LLM may still mark it as
the loser of the comparison).

| Tag | What triggers it |
|---|---|
| `hallucinated_tool` | A `ToolResult` carries an error matching `no tool named`, `invalid arguments`, or `not permitted`. |
| `infinite_loop` | Three consecutive `ToolUse` events with identical `(tool_name, arguments)`. Different arguments don't trigger it — that's normal "tweak and retry." |
| `off_task` | No `final_answer`, or final answer < 30 chars (the agent stalled). |
| `refusal` | Final answer matches refusal patterns ("I can't", "I'm not able to"). Takes precedence over `off_task` so reports don't double-tag. |
| `cost_blowup` | More than 50 tool calls. A proxy until per-run cost is recorded in the trajectory. |

The thresholds are constants in
[`src/harness_weaver/judge/classifier.py`](../../src/harness_weaver/judge/classifier.py).
Tune in your fork; tests reference the constants so they don't go
stale.

## Success criteria

If the Task carries `success_criteria`, the structural report
pass/fails each one. Recognised keys (extend in
[`src/harness_weaver/judge/structural.py`](../../src/harness_weaver/judge/structural.py)):

| Key | Type | Check |
|---|---|---|
| `min_results` | `int` | At least N hits returned across all search calls. |
| `max_results` | `int` | At most N hits. |
| `max_runtime_minutes` | `int` | Every hit has `runtime_minutes <= value`. |
| `min_runtime_minutes` | `int` | Every hit has `runtime_minutes >= value`. |
| `min_rating` | `float` | Every hit has `rating >= value`. |
| `must_include_genre` | `str` | Every hit lists this genre. |

Unrecognised keys land as `"unknown"` rather than passing or failing
silently — so you see what wasn't checked, and the LLM judge can still
reason about them.

## The LLM judge prompt

The system prompt asks the model to evaluate on this rubric, in
priority order:

1. **Task fidelity** — did the agent address what was asked?
2. **Grounding** — are answer claims supported by tool results?
3. **Tool use efficiency** — reasonable calls or thrashing?
4. **Final answer quality** — clarity, justification, fit.

Output is JSON: `{"winner": "a"|"b"|"tie"|"both_fail", "reasoning":
str, "confidence": 0..1}`. The CLI parses and writes it to
`{task_id}.compare.verdict.json`.

The full prompt lives in
[`src/harness_weaver/judge/llm.py`](../../src/harness_weaver/judge/llm.py).
Edit it there if you want different priorities.

## Choosing a judge model

The default is `anthropic/claude-haiku-4-5-20251001` — cheap,
credible. Override at the CLI:

```bash
--judge-model anthropic/claude-sonnet-4-6
```

Pass any inspect-ai-recognized model id. The judge logic is
model-agnostic — the verdict shape doesn't change.

## Programmatic use

Drive the judge directly from Python; useful for batch scoring or
custom pipelines:

```python
import asyncio

from harness_weaver.judge import (
    InspectAILlmJudge,
    StructuralReport,
    render_markdown,
)
from harness_weaver.task import Task
from harness_weaver.trajectory import Trajectory

task = Task.from_path("examples/tasks/discovery-mood-tense.json")
a = Trajectory.model_validate_json(open("trajectory-a.json").read())
b = Trajectory.model_validate_json(open("trajectory-b.json").read())

# Cheap layer:
print(render_markdown(StructuralReport.of(a, b, task=task)))

# Paid layer (needs ANTHROPIC_API_KEY):
judge = InspectAILlmJudge(model="anthropic/claude-haiku-4-5-20251001")
verdict = asyncio.run(judge.verdict(task=task, trajectory_a=a, trajectory_b=b))
print(f"winner: {verdict.winner} ({verdict.confidence:.2f})")
print(verdict.reasoning)
```

For tests, swap `InspectAILlmJudge` for `FixedJudge` — same protocol,
canned verdict, no API call.

## What's not yet here

* **Calibration** — the README's design notes promise a small
  human-rated set the judge gets calibrated against. Not built yet.
  When it lands, the judge's `confidence` field becomes meaningful
  beyond self-report.
* **Aggregation across a TaskPack** — `harness-weaver eval` writes
  per-task trajectories but doesn't yet aggregate verdicts into a
  pack-level report.
* **Judging without comparison** — the current rubric is intrinsically
  pairwise (a vs b). A single-trajectory quality verdict is a
  separate prompt and not yet implemented.

Back to the [manual index](README.md).

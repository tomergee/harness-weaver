# Web UI

A flat HTML interface for kicking off runs and browsing trajectories.
Optional — install the `web` extra and start `harness-weaver serve`.

The UI is intentionally minimal: server-rendered Jinja2 templates, one
small CSS file, no JS framework, no auth. It's a "click around and look
at things" demo surface, not production UX.

## Install

The web stack ships as an optional extra so the core install stays
lean:

```bash
pip install -e ".[web]"
```

That pulls in `fastapi`, `uvicorn`, `jinja2`, `python-multipart`,
`markdown`, and `bleach`. If you already installed with `[dev]`, add
`[dev,web]`.

## Start the server

```bash
harness-weaver serve --host 127.0.0.1 --port 8000
```

Then open <http://127.0.0.1:8000>. The default bind is `127.0.0.1`
because the UI has no auth — anyone who can reach the port can kick off
runs that hit your API key. If you want it on the network, that's your
call; pass `--host 0.0.0.0` and put it behind something.

`--runs-dir` controls where trajectories and reports are read from and
written to (default `runs/`). It's the same shape the CLI uses, so
existing `harness-weaver run` outputs show up automatically.

## Pages

| Path                          | What it does |
|-------------------------------|---|
| `/`                           | Lists trajectories (`*.json`) and reports (`*.md`) from the runs directory. Cards link to the three forms below. |
| `/runs/new`                   | Form: pick task, configuration, optional model override, optional `--use-k8s`. The right-hand sidebar shows what each configuration uses (tools, model, agent topology, system-prompt excerpt) and updates as you change the picker. Submitting redirects to the live job page. |
| `/compare/new`                | Form: pick task and two configurations. Optional `judge_model` opts in to an LLM verdict. Same sidebar pattern, twice (one panel per leg). Submitting redirects to the live job page. |
| `/eval/new`                   | Form: pick a task pack and a configuration. Submitting redirects to the live job page. |
| `/jobs/{id}`                  | Live status for a submitted job: planned step list (numbered, with descriptions), elapsed-time counter, and a dark-themed event log fed by an SSE stream. Auto-redirects to the trajectory or report when the job finishes. |
| `/jobs/{id}.json`             | Same data as the live page, but as JSON. Used by the test suite. |
| `/jobs/{id}/events`           | Server-Sent Events stream feeding the live page. One `data:` block per phase transition; closes with an `event: done` carrying the final snapshot when the job is terminal. |
| `/trajectories/{filename}`    | Renders a trajectory JSON readably: header (task / config / cost / turns), final answer block, timeline of events with type badges. |
| `/reports/{filename}`         | Renders a markdown report file as HTML. Used for compare and eval outputs. Surfaces the LLM verdict next to the report when `--judge-model` was used. |

## Job lifecycle, by job type

Each job is a sequence of phases. The job page paints all of them
greyed out at the start, then walks them through *pending → running →
done* (or *error*) as the worker progresses. Each transition is also
appended to the live event log with a timestamp and a one-line detail.

**run.** Five phases:

1. `load-task` — read the task JSON, validate as a pydantic Task.
2. `resolve-config` — look up the configuration by name; apply the
   model override via `Configuration.model_copy`.
3. `build-harness` — Harness + catalog + `RealAgentRunner`; selects
   `LocalSubprocessBackend` or `AgentSandboxBackend` based on the form.
4. `sdk-call` — compile to `ClaudeAgentOptions`, build the in-process
   MCP server, drive `claude_agent_sdk.query()` until the agent emits a
   final answer. Most of the wall-clock time lives here.
5. `write-output` — serialize the trajectory to
   `runs/{task_id}.{config_name}.json` and redirect.

**compare.** Eight phases. Same scaffolding as `run`, but two
back-to-back SDK calls (`sdk-call-a`, `sdk-call-b`), then a
`structural-report` phase computing the rules-based diff, then an
optional `judge` phase (skipped without `judge_model`).

**eval.** Variable: `load-pack`, `resolve-config`, `build-harness`,
then one `sdk-call-N` step per task in the pack, then `aggregate`
(pack-level summary), then `write-output`. The job page knows the
pack size at submit time and renders the right number of step rows.

## Threading model and limits

* **Single-worker `ThreadPoolExecutor`.** Concurrent submissions queue
  up rather than fighting for the API key budget. If you click *Run*
  in two tabs, the second's job page sits at *queued* until the first
  is done.
* **In-memory job state.** Restart the server and the live job pages
  go away. The *artifacts* (trajectory JSON, report markdown, verdict
  JSON) are still on disk under `runs_dir` — the browse pages still
  show them.
* **SSE polling cadence.** The endpoint walks the in-memory event log
  every 200 ms, with a 3 s heartbeat comment between phases so proxy
  buffering doesn't swallow the stream when the SDK call is quiet.

## Caveats — read these before showing the UI to anyone

* **No auth.** Bind to `127.0.0.1` — the server happily kicks off paid
  runs for anyone who can reach the port.
* **Read-only browsing of `runs/`.** No DB. Trajectories and reports
  stay JSON and Markdown files on disk. To delete one, `rm` it.
* **Path traversal is refused.** The form's task picker is bound to
  files under `examples/`, and the trajectory/report viewers refuse
  paths that escape the runs directory.
* **Markdown is sanitized with `bleach`.** Reports embed trajectory
  snippets and LLM output, both untrusted. The HTML output of the
  markdown renderer goes through a narrow tag/attr allowlist before
  reaching the page — no `<script>`, no inline event handlers.

## Programmatic use

The CLI's `serve` command is just `uvicorn.run(create_app(...))` —
import the factory yourself if you want to wire it into a different
stack:

```python
from harness_weaver.web import create_app

app = create_app(runs_dir="custom_runs")  # FastAPI app
# Then run with whatever ASGI server you prefer.
```

`create_app` accepts a `harness_factory` callable — that's the seam
the test suite uses to inject a fake `Harness`. Production
(`DefaultHarnessFactory`) builds the same `Harness + RealAgentRunner`
the CLI uses, including K8s backend lifecycle when `use_k8s=True` on
the request.

## Tests

`tests/test_web_app.py` uses FastAPI's `TestClient` against a
`create_app(...)` instance with a fake harness factory. No SDK calls,
no network. The whole module skips cleanly when the `web` extras
aren't installed (`pytest.importorskip` on `fastapi`, `jinja2`,
`markdown`, `multipart`, `httpx`).

Back to the [manual index](README.md).

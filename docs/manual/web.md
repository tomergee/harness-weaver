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

That pulls in `fastapi`, `uvicorn`, `jinja2`, `python-multipart`, and
`markdown`. If you already installed with `[dev]`, add `[dev,web]`.

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
| `/runs/new`                   | Form: pick task, configuration, optional model override, optional `--use-k8s`. Submitting blocks the browser until the run finishes (~20-60s live), then redirects to the trajectory view. |
| `/compare/new`                | Form: pick task and two configurations. Optional `judge_model` opts in to an LLM verdict. Redirects to the comparison report. |
| `/eval/new`                   | Form: pick a task pack and a configuration. Runs every task in the pack, redirects to the markdown summary. |
| `/trajectories/{filename}`    | Renders a trajectory JSON readably: header (task / config / cost / turns), final answer block, timeline of events with type badges. |
| `/reports/{filename}`         | Renders a markdown report file as HTML. Used for compare and eval outputs. |

## Caveats — read these before showing the UI to anyone

* **The browser blocks for the duration of every run.** No streaming
  progress, no background queue. A `live-eval` over an 8-task pack with
  Haiku takes a couple of minutes; the page sits there until the
  uvicorn worker finishes. This is fine for a single-user demo and
  intentional for v1; if you want SSE/WebSocket streaming, it's a
  separate project.
* **Single uvicorn worker, sync execution.** Concurrent submissions
  serialize. If you click *Run* in two tabs, the second waits for the
  first.
* **No auth.** Bind to `127.0.0.1` — the server happily kicks off paid
  runs for anyone who can reach the port.
* **Read-only browsing of `runs/`.** No DB. Trajectories and reports
  stay JSON and Markdown files on disk. To delete one, `rm` it.
* **Path traversal is refused.** The form's task picker is bound to
  files under `examples/`, and the trajectory/report viewers refuse
  paths that escape the runs directory.

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

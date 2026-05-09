"""FastAPI app factory for the web UI.

Pages, all server-rendered:

* ``/``                          — landing page; lists existing trajectories
                                   and reports under ``runs/``, links to the
                                   forms below.
* ``/runs/new``                  — form to kick off a single run. Picking a
                                   configuration shows its tools, model, and
                                   agent topology in a live sidebar.
* ``/compare/new``               — form to run two configurations side by
                                   side. Same sidebar pattern, plus an
                                   optional ``judge_model`` field.
* ``/eval/new``                  — form to evaluate one configuration over a
                                   task pack.
* ``/jobs/{id}``                 — live status page for a submitted job.
                                   Shows the planned step list, an elapsed
                                   timer, and an SSE-fed log of phase events.
                                   Auto-redirects to the trajectory or report
                                   when the job finishes.
* ``/jobs/{id}/events``          — Server-Sent Events stream feeding the live
                                   status page. Yields one event per phase
                                   transition; closes when the job is terminal.
* ``/trajectories/{filename}``   — renders a trajectory JSON file readably
                                   (timeline, final answer, cost / turns /
                                   duration).
* ``/reports/{filename}``        — renders a markdown report file as HTML.

Form submissions used to block the browser for the duration of the
harness call (20-90s). They now enqueue a :class:`Job` on a single-worker
ThreadPoolExecutor and 303-redirect to ``/jobs/{id}``; the job page
opens an EventSource on the SSE endpoint and renders progress live.

No auth. Bind to 127.0.0.1.

The harness instance is constructed via the :class:`HarnessFactory`
protocol — production wires :class:`DefaultHarnessFactory` (which builds
real Harnesses with ``RealAgentRunner``); tests inject a fake.
"""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import bleach
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from harness_weaver.catalog import Catalog
from harness_weaver.configurations import builtin_configurations
from harness_weaver.harness import Harness
from harness_weaver.web.jobs import JobRegistry, configuration_summary

# --- Harness factory seam ---------------------------------------------------


class HarnessFactory(Protocol):
    """Constructs a Harness for a single web request.

    Production builds a real Harness with ``RealAgentRunner``; tests
    inject a fake that returns a pre-baked trajectory without touching
    the SDK or the network.
    """

    def __call__(self, *, use_k8s: bool, k8s_namespace: str) -> "_HarnessCtx": ...


class _HarnessCtx(Protocol):
    """A context-manager wrapper around a Harness.

    The wrapping is what lets the K8s backend close cleanly: the
    DefaultHarnessFactory yields a real Harness inside a ``with`` block
    that owns the AgentSandboxBackend lifecycle.
    """

    def __enter__(self) -> Harness: ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...


@dataclass
class DefaultHarnessFactory:
    """Production factory: builds a Harness with the real SDK runner.

    Mirrors the CLI's ``_build_harness`` context manager: when
    ``use_k8s=True``, swaps in :class:`AgentSandboxBackend` and
    closes it on exit so we don't leak pods.
    """

    def __call__(self, *, use_k8s: bool, k8s_namespace: str) -> "_HarnessCtx":
        return _DefaultHarnessCtx(use_k8s=use_k8s, k8s_namespace=k8s_namespace)


class _DefaultHarnessCtx:
    def __init__(self, *, use_k8s: bool, k8s_namespace: str) -> None:
        self._use_k8s = use_k8s
        self._k8s_namespace = k8s_namespace
        self._backend: Any = None

    def __enter__(self) -> Harness:
        from harness_weaver.agent_runner import RealAgentRunner

        if not self._use_k8s:
            return Harness(catalog=Catalog.load_default(), runner=RealAgentRunner())

        from harness_weaver.execution import AgentSandboxBackend

        self._backend = AgentSandboxBackend(namespace=self._k8s_namespace)
        return Harness(
            catalog=Catalog.load_default(),
            runner=RealAgentRunner(),
            execution_backend=self._backend,
        )

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        if self._backend is not None:
            self._backend.close()


# --- App factory ------------------------------------------------------------


def _list_runs_dir(runs_dir: Path) -> tuple[list[Path], list[Path]]:
    """Return ``(trajectories, reports)`` from ``runs_dir``, sorted newest first."""
    if not runs_dir.exists():
        return [], []
    trajectories = sorted(
        (p for p in runs_dir.iterdir() if p.is_file() and p.suffix == ".json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    reports = sorted(
        (p for p in runs_dir.iterdir() if p.is_file() and p.suffix == ".md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return trajectories, reports


def _list_examples(repo_root: Path) -> tuple[list[Path], list[Path]]:
    """Return ``(task_files, pack_files)`` from ``examples/``."""
    tasks_dir = repo_root / "examples" / "tasks"
    packs_dir = repo_root / "examples" / "packs"
    tasks = sorted(tasks_dir.glob("*.json")) if tasks_dir.exists() else []
    packs = sorted(packs_dir.glob("*.json")) if packs_dir.exists() else []
    return tasks, packs


def _safe_join(base: Path, name: str) -> Path | None:
    """Join ``name`` under ``base``; return None if it escapes the base.

    Path traversal protection: a user could ask for
    ``/trajectories/../../etc/passwd`` and we want to refuse.
    """
    try:
        candidate = (base / name).resolve()
        base_resolved = base.resolve()
    except (OSError, ValueError):
        return None
    if base_resolved not in candidate.parents and candidate != base_resolved:
        return None
    if not candidate.is_file():
        return None
    return candidate


def _find_repo_root() -> Path:
    """Best-effort discovery of a harness-weaver checkout.

    Walks up from the current working directory looking for an
    ``examples/`` sibling. Falls back to ``Path.cwd()`` if nothing
    matches — the form pickers will simply be empty in that case
    rather than blowing up. The previous ``parents[3]`` heuristic
    only worked when the package was installed as an editable source
    checkout (PR #11 review): wheel installs put us at
    ``site-packages/harness_weaver/web/app.py``, where ``parents[3]``
    is some random directory that won't have ``examples/``.
    """
    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "examples").is_dir():
            return candidate
    return Path.cwd()


# Bleach allowlist for the markdown -> HTML pipeline. Reports embed
# trajectory snippets and LLM output (PR #11 review on XSS), both
# untrusted. Stick to a narrow tag set that covers `tables` and
# `fenced_code` extensions and nothing scriptable.
_ALLOWED_TAGS: frozenset[str] = frozenset(
    {
        "a",
        "blockquote",
        "br",
        "code",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "strong",
        "table",
        "tbody",
        "td",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
_ALLOWED_ATTRS: dict[str, list[str]] = {
    "a": ["href", "title"],
    "code": ["class"],  # for fenced_code language hints
    "pre": ["class"],
}


_ERROR_MESSAGES: dict[str, str] = {
    "bad_task_path": (
        "Task path must be inside the repository — paths outside the "
        "checkout (or absolute paths to system files) are rejected."
    ),
    "bad_pack_path": (
        "Pack path must be inside the repository — paths outside the "
        "checkout (or absolute paths to system files) are rejected."
    ),
}


def _humanize_error(code: str) -> str:
    """Map a redirect ``error=`` code to a human-readable message.

    Returns the empty string for unknown / missing codes so templates
    can use ``{% if error %}…`` cleanly.
    """
    if not code:
        return ""
    return _ERROR_MESSAGES.get(code, f"Unknown error: {code}")


def _load_verdict_for_report(runs_dir: Path, report_filename: str) -> dict[str, Any] | None:
    """If a sibling verdict JSON exists for a compare report, load it.

    The compare flow writes ``<task_id>.compare.md`` *and* (when
    ``judge_model`` was set) ``<task_id>.compare.verdict.json``. The
    user paid for the verdict — we should not silently drop it just
    because the redirect target is the markdown file. PR #11 review.
    """
    if not report_filename.endswith(".compare.md"):
        return None
    verdict_name = report_filename.removesuffix(".md") + ".verdict.json"
    verdict_path = _safe_join(runs_dir, verdict_name)
    if verdict_path is None:
        return None
    try:
        loaded = json.loads(verdict_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def create_app(
    *,
    runs_dir: Path | str = "runs",
    repo_root: Path | str | None = None,
    harness_factory: HarnessFactory | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    Args:
        runs_dir: Directory the UI reads/writes trajectories and reports
            from. Created on first use. Defaults to ``runs/`` relative
            to CWD.
        repo_root: Root used to discover example tasks and packs (under
            ``examples/``). Defaults to a walk-up from CWD looking for
            an ``examples/`` directory; falls back to CWD. Wheel
            installs without a checkout will see empty pickers, which
            is preferable to crashing.
        harness_factory: Constructs a Harness per request. Defaults to
            :class:`DefaultHarnessFactory`. Tests inject a fake.
    """
    runs_dir_path = Path(runs_dir)
    repo_root_path = Path(repo_root) if repo_root is not None else _find_repo_root()
    factory: HarnessFactory = harness_factory or DefaultHarnessFactory()

    # The job registry owns the worker thread pool. Single-worker so jobs
    # serialize — the harness's sync API stays sync, and concurrent
    # submissions from a frantically-clicking user queue up rather than
    # fighting for the API key budget.
    registry = JobRegistry(
        repo_root=repo_root_path,
        runs_dir=runs_dir_path,
        factory=factory,
    )

    here = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=str(here / "templates"))

    app = FastAPI(
        title="harness-weaver",
        docs_url=None,  # don't expose /docs by default; the UI is the docs
        redoc_url=None,
    )
    app.mount(
        "/static",
        StaticFiles(directory=str(here / "static")),
        name="static",
    )

    @app.on_event("shutdown")
    def _shutdown() -> None:
        # Give the worker pool a chance to drain on a clean uvicorn stop;
        # in-flight harness calls can't be cancelled mid-SDK-stream so
        # we accept that ctrl-c during a run leaves the worker running
        # until it returns. PR #18 review.
        registry.shutdown()

    # --- pages -------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Any:
        trajectories, reports = _list_runs_dir(runs_dir_path)
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "trajectories": [p.name for p in trajectories],
                "reports": [p.name for p in reports],
            },
        )

    def _config_summaries() -> dict[str, Any]:
        """Pre-computed configuration metadata for the form sidebar.

        Embedded as JSON in the form templates; the JS sidebar reads
        from a hidden ``<script type="application/json">`` rather than
        making a fetch round-trip on every selection.
        """
        return {cfg.name: configuration_summary(cfg.name) for cfg in builtin_configurations()}

    def _path_under_repo(rel_path: str) -> Path | None:
        """Resolve ``rel_path`` against the repo root and reject escapes."""
        candidate = (repo_root_path / rel_path).resolve()
        if repo_root_path not in candidate.parents:
            return None
        return candidate

    @app.get("/runs/new", response_class=HTMLResponse)
    def runs_new_form(request: Request, error: str = "") -> Any:
        tasks, _ = _list_examples(repo_root_path)
        return templates.TemplateResponse(
            request,
            "runs_new.html",
            {
                "tasks": [str(p.relative_to(repo_root_path)) for p in tasks],
                "configs": list(builtin_configurations()),
                "config_summaries": _config_summaries(),
                "error": _humanize_error(error),
            },
        )

    @app.post("/runs/new")
    def runs_new_submit(
        task: str = Form(...),
        config: str = Form(...),
        model: str = Form(""),
        use_k8s: bool = Form(False),
        k8s_namespace: str = Form("default"),
    ) -> Any:
        if _path_under_repo(task) is None:
            return RedirectResponse(url="/runs/new?error=bad_task_path", status_code=303)
        job = registry.submit_run(
            {
                "task": task,
                "config": config,
                "model": model,
                "use_k8s": use_k8s,
                "k8s_namespace": k8s_namespace,
            }
        )
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)

    @app.get("/compare/new", response_class=HTMLResponse)
    def compare_new_form(request: Request, error: str = "") -> Any:
        tasks, _ = _list_examples(repo_root_path)
        return templates.TemplateResponse(
            request,
            "compare_new.html",
            {
                "tasks": [str(p.relative_to(repo_root_path)) for p in tasks],
                "configs": list(builtin_configurations()),
                "config_summaries": _config_summaries(),
                "error": _humanize_error(error),
            },
        )

    @app.post("/compare/new")
    def compare_new_submit(
        task: str = Form(...),
        config_a: str = Form(...),
        config_b: str = Form(...),
        model: str = Form(""),
        judge_model: str = Form(""),
        use_k8s: bool = Form(False),
        k8s_namespace: str = Form("default"),
    ) -> Any:
        if _path_under_repo(task) is None:
            return RedirectResponse(url="/compare/new?error=bad_task_path", status_code=303)
        job = registry.submit_compare(
            {
                "task": task,
                "config_a": config_a,
                "config_b": config_b,
                "model": model,
                "judge_model": judge_model,
                "use_k8s": use_k8s,
                "k8s_namespace": k8s_namespace,
            }
        )
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)

    @app.get("/eval/new", response_class=HTMLResponse)
    def eval_new_form(request: Request, error: str = "") -> Any:
        _, packs = _list_examples(repo_root_path)
        return templates.TemplateResponse(
            request,
            "eval_new.html",
            {
                "packs": [str(p.relative_to(repo_root_path)) for p in packs],
                "configs": list(builtin_configurations()),
                "config_summaries": _config_summaries(),
                "error": _humanize_error(error),
            },
        )

    @app.post("/eval/new")
    def eval_new_submit(
        pack: str = Form(...),
        config: str = Form(...),
        model: str = Form(""),
        use_k8s: bool = Form(False),
        k8s_namespace: str = Form("default"),
    ) -> Any:
        if _path_under_repo(pack) is None:
            return RedirectResponse(url="/eval/new?error=bad_pack_path", status_code=303)
        job = registry.submit_eval(
            {
                "pack": pack,
                "config": config,
                "model": model,
                "use_k8s": use_k8s,
                "k8s_namespace": k8s_namespace,
            }
        )
        return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)

    # --- Job pages ---------------------------------------------------------
    #
    # Route order matters here: ``/jobs/{job_id}`` matches
    # ``/jobs/abc.json`` because ``{job_id}`` accepts anything except a
    # slash. Declare the more specific JSON + SSE routes first so they
    # win the match.

    @app.get("/jobs/{job_id}.json")
    def job_json(job_id: str) -> Any:
        job = registry.get(job_id)
        if job is None:
            return HTMLResponse("Job not found", status_code=404)
        return job.snapshot()

    @app.get("/jobs/{job_id}/events")
    async def job_events(job_id: str) -> Any:
        """Server-Sent Events stream for the live job page.

        Polls ``job.events`` from the worker thread on a 200 ms cadence.
        Each new entry becomes one ``data:`` block. When the job
        terminates we send one final ``event: done`` so the browser
        knows to stop and follow the redirect URL.
        """
        job = registry.get(job_id)
        if job is None:
            return HTMLResponse("Job not found", status_code=404)

        async def stream() -> Any:
            cursor = 0
            # Heartbeat every ~3s so proxies / browsers don't time out
            # when the SDK call is long and quiet between phase events.
            ticks_since_heartbeat = 0
            while True:
                # Hold the lock only long enough to copy whatever new
                # event records appeared since our cursor (PR #18 review:
                # an async function holding RLock blocks the event loop;
                # keeping it short is the cheap fix without dragging in a
                # cross-thread asyncio.Queue).
                with job._lock:
                    new = list(job.events[cursor:])
                    cursor = len(job.events)
                # Capture terminal state independently of whether new
                # events landed in this tick. PR #18 review: if the
                # worker finished between two ticks without emitting,
                # the previous "snapshot if new" guard kept the loop
                # spinning forever.
                is_done = job.is_terminal()
                for event in new:
                    yield f"data: {json.dumps(event.to_dict())}\n\n"
                if is_done:
                    yield f"event: done\ndata: {json.dumps(job.snapshot())}\n\n"
                    break
                ticks_since_heartbeat += 1
                if ticks_since_heartbeat >= 15:  # ~3s at 200ms tick
                    yield ": heartbeat\n\n"
                    ticks_since_heartbeat = 0
                await asyncio.sleep(0.2)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable nginx-style proxy buffering
            },
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_view(request: Request, job_id: str) -> Any:
        job = registry.get(job_id)
        if job is None:
            return HTMLResponse("Job not found", status_code=404)
        return templates.TemplateResponse(
            request,
            "job.html",
            {"job": job.snapshot()},
        )

    @app.get("/trajectories/{filename}", response_class=HTMLResponse)
    def trajectory_view(request: Request, filename: str) -> Any:
        path = _safe_join(runs_dir_path, filename)
        if path is None:
            return HTMLResponse("Not found", status_code=404)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return HTMLResponse(f"Could not read trajectory: {exc}", status_code=400)
        return templates.TemplateResponse(
            request,
            "trajectory.html",
            {"filename": filename, "trajectory": data},
        )

    @app.get("/reports/{filename}", response_class=HTMLResponse)
    def report_view(request: Request, filename: str) -> Any:
        import markdown as md

        path = _safe_join(runs_dir_path, filename)
        if path is None:
            return HTMLResponse("Not found", status_code=404)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            return HTMLResponse(f"Could not read report: {exc}", status_code=400)
        # Markdown content embeds trajectory snippets and LLM output.
        # Bleach sanitizes any HTML the markdown renderer emits — narrow
        # tag/attr allowlist, no <script>, no inline event handlers,
        # nothing scriptable. PR #11 review on XSS.
        unsafe_html = md.markdown(text, extensions=["tables", "fenced_code"])
        rendered_html = bleach.clean(
            unsafe_html,
            tags=_ALLOWED_TAGS,
            attributes=_ALLOWED_ATTRS,
            strip=True,
        )

        # If a compare verdict was written for the same task id, surface
        # it next to the report — otherwise an opt-in --judge-model run
        # silently writes a JSON file the user never sees (PR #11 review).
        verdict = _load_verdict_for_report(runs_dir_path, filename)

        return templates.TemplateResponse(
            request,
            "report.html",
            {
                "filename": filename,
                "rendered_html": rendered_html,
                "verdict": verdict,
            },
        )

    return app

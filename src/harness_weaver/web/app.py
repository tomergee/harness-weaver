"""FastAPI app factory for the web UI.

Five pages, all server-rendered:

* ``/``                          — landing page; lists existing trajectories
                                   and reports under ``runs/``, links to the
                                   forms below.
* ``/runs/new``                  — form to kick off a single run; submitting
                                   blocks the browser until the run finishes
                                   (~20-60s live), then redirects to the
                                   trajectory view.
* ``/compare/new``               — form to run two configurations side by
                                   side; emits a structural report (and an
                                   LLM verdict when ``judge_model`` is set).
* ``/eval/new``                  — form to evaluate one configuration over a
                                   task pack; emits a markdown summary.
* ``/trajectories/{filename}``   — renders a trajectory JSON file readably
                                   (timeline, final answer, cost / turns /
                                   duration).
* ``/reports/{filename}``        — renders a markdown report file as HTML.

Single uvicorn worker, sync execution. No streaming, no background queue,
no auth. Bind to 127.0.0.1; if you expose this on a network you take the
risk yourself.

The harness instance is constructed via the :class:`HarnessFactory`
protocol — production wires :class:`DefaultHarnessFactory` (which builds
real Harnesses with ``RealAgentRunner``); tests inject a fake.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from harness_weaver.catalog import Catalog
from harness_weaver.configurations import (
    Configuration,
    builtin_configurations,
    configuration_by_name,
)
from harness_weaver.harness import Harness
from harness_weaver.task import Task, TaskPack

if TYPE_CHECKING:  # pragma: no cover
    from harness_weaver.trajectory import Trajectory

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


def _resolve_config(name: str, model_override: str | None) -> Configuration:
    cfg = configuration_by_name(name)
    if model_override is None or model_override == "":
        return cfg
    return cfg.model_copy(update={"model": model_override})


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


@contextmanager
def _runs_output(runs_dir: Path) -> Iterator[Path]:
    runs_dir.mkdir(parents=True, exist_ok=True)
    yield runs_dir


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
            ``examples/``). Defaults to the harness-weaver source repo
            root inferred from this module's location.
        harness_factory: Constructs a Harness per request. Defaults to
            :class:`DefaultHarnessFactory`. Tests inject a fake.
    """
    runs_dir_path = Path(runs_dir)
    repo_root_path = (
        Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[3]
    )
    factory: HarnessFactory = harness_factory or DefaultHarnessFactory()

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

    @app.get("/runs/new", response_class=HTMLResponse)
    def runs_new_form(request: Request) -> Any:
        tasks, _ = _list_examples(repo_root_path)
        return templates.TemplateResponse(
            request,
            "runs_new.html",
            {
                "tasks": [str(p.relative_to(repo_root_path)) for p in tasks],
                "configs": list(builtin_configurations()),
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
        cfg = _resolve_config(config, model)
        task_path = (repo_root_path / task).resolve()
        # Reject paths that aren't under the repo root or examples/.
        if repo_root_path not in task_path.parents:
            return RedirectResponse(url="/runs/new?error=bad_task_path", status_code=303)
        task_obj = Task.from_path(task_path)
        with factory(use_k8s=use_k8s, k8s_namespace=k8s_namespace) as harness:
            trajectory = harness.run(task_obj, cfg)
        with _runs_output(runs_dir_path) as out:
            out_path = out / f"{trajectory.task_id}.{cfg.name}.json"
            out_path.write_text(trajectory.model_dump_json(indent=2), encoding="utf-8")
        return RedirectResponse(url=f"/trajectories/{out_path.name}", status_code=303)

    @app.get("/compare/new", response_class=HTMLResponse)
    def compare_new_form(request: Request) -> Any:
        tasks, _ = _list_examples(repo_root_path)
        return templates.TemplateResponse(
            request,
            "compare_new.html",
            {
                "tasks": [str(p.relative_to(repo_root_path)) for p in tasks],
                "configs": list(builtin_configurations()),
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
        from harness_weaver.judge import StructuralReport, render_markdown

        cfg_a = _resolve_config(config_a, model)
        cfg_b = _resolve_config(config_b, model)
        task_path = (repo_root_path / task).resolve()
        if repo_root_path not in task_path.parents:
            return RedirectResponse(url="/compare/new?error=bad_task_path", status_code=303)
        task_obj = Task.from_path(task_path)
        trajs: list[Trajectory] = []
        with factory(use_k8s=use_k8s, k8s_namespace=k8s_namespace) as harness:
            for cfg in (cfg_a, cfg_b):
                trajectory = harness.run(task_obj, cfg)
                with _runs_output(runs_dir_path) as out:
                    (out / f"{trajectory.task_id}.{cfg.name}.json").write_text(
                        trajectory.model_dump_json(indent=2), encoding="utf-8"
                    )
                trajs.append(trajectory)
        report = StructuralReport.of(trajs[0], trajs[1], task=task_obj)
        with _runs_output(runs_dir_path) as out:
            report_path = out / f"{task_obj.task_id}.compare.md"
            report_path.write_text(render_markdown(report), encoding="utf-8")
        if judge_model:
            import asyncio

            from harness_weaver.judge.llm import InspectAILlmJudge

            judge = InspectAILlmJudge(model=judge_model)
            verdict = asyncio.run(
                judge.verdict(
                    task=task_obj,
                    trajectory_a=trajs[0],
                    trajectory_b=trajs[1],
                )
            )
            with _runs_output(runs_dir_path) as out:
                (out / f"{task_obj.task_id}.compare.verdict.json").write_text(
                    verdict.model_dump_json(indent=2), encoding="utf-8"
                )
        return RedirectResponse(url=f"/reports/{report_path.name}", status_code=303)

    @app.get("/eval/new", response_class=HTMLResponse)
    def eval_new_form(request: Request) -> Any:
        _, packs = _list_examples(repo_root_path)
        return templates.TemplateResponse(
            request,
            "eval_new.html",
            {
                "packs": [str(p.relative_to(repo_root_path)) for p in packs],
                "configs": list(builtin_configurations()),
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
        from harness_weaver.judge import PackSummary, render_pack_markdown

        cfg = _resolve_config(config, model)
        pack_path = (repo_root_path / pack).resolve()
        if repo_root_path not in pack_path.parents:
            return RedirectResponse(url="/eval/new?error=bad_pack_path", status_code=303)
        pack_obj = TaskPack.from_path(pack_path)
        trajs: list[Trajectory] = []
        with factory(use_k8s=use_k8s, k8s_namespace=k8s_namespace) as harness:
            for task_obj in pack_obj.tasks:
                trajectory = harness.run(task_obj, cfg)
                with _runs_output(runs_dir_path) as out:
                    (out / f"{trajectory.task_id}.{cfg.name}.json").write_text(
                        trajectory.model_dump_json(indent=2), encoding="utf-8"
                    )
                trajs.append(trajectory)
        summary = PackSummary.of(trajs, pack=pack_obj, configuration_name=cfg.name)
        with _runs_output(runs_dir_path) as out:
            summary_path = out / f"{pack_obj.name}.{cfg.name}.eval.md"
            summary_path.write_text(render_pack_markdown(summary), encoding="utf-8")
        return RedirectResponse(url=f"/reports/{summary_path.name}", status_code=303)

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
        html = md.markdown(text, extensions=["tables", "fenced_code"])
        return templates.TemplateResponse(
            request,
            "report.html",
            {"filename": filename, "rendered_html": html},
        )

    return app

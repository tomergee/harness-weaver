"""Command-line interface for harness-weaver.

Three subcommands:

* ``run``     execute one task with one configuration; write the trajectory.
* ``compare`` run the same task under two configurations; write both
              trajectories. (Judge integration lands separately.)
* ``eval``    run a configuration over a task pack; write per-task
              trajectories. (Judge integration lands separately.)

The CLI wires the production stack: the bundled catalog, a fresh
:class:`LocalSubprocessBackend`, and :class:`RealAgentRunner`. Live runs
require ``ANTHROPIC_API_KEY`` (or whatever credential the
``claude-agent-sdk`` picks up from its environment).

For programmatic use without the SDK in the loop, instantiate
:class:`Harness` directly with a :class:`FakeAgentRunner`; this is the
path the e2e tests exercise.
"""

import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from dotenv import load_dotenv
from rich.console import Console

from harness_weaver import __version__
from harness_weaver.agent_runner import RealAgentRunner
from harness_weaver.catalog import Catalog
from harness_weaver.configurations import builtin_configurations, configuration_by_name
from harness_weaver.harness import Harness
from harness_weaver.task import Task, TaskPack

if TYPE_CHECKING:
    from harness_weaver.configurations import Configuration


def _discover_repo_root() -> Path | None:
    """Return the harness-weaver checkout root (contains pyproject.toml), if known."""
    here = Path(__file__).resolve()
    for d in (here.parent, *here.parents):
        marker = d / "pyproject.toml"
        if not marker.is_file():
            continue
        try:
            with marker.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        project = data.get("project")
        if isinstance(project, dict) and project.get("name") == "harness-weaver":
            return d
    return None


def _load_dotenv_files() -> None:
    """Load a single .env file: prefer cwd, then repo root, then repo parent.

    Stops at the first path that exists. ``override=False`` keeps keys already
    set in the process environment (e.g. exported in the shell).
    """
    cwd = Path.cwd()
    candidates: list[Path] = [cwd / ".env"]
    root = _discover_repo_root()
    if root is not None:
        candidates.append(root / ".env")
        candidates.append(root.parent / ".env")
    for path in candidates:
        if path.is_file():
            load_dotenv(path, override=False)
            return


app = typer.Typer(
    name="harness-weaver",
    help="Experimentation harness for agentic systems on recommendation-style tasks.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"harness-weaver {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Print version and exit.",
        ),
    ] = False,
) -> None:
    """harness-weaver entry point."""
    del version  # consumed by the eager callback above
    _load_dotenv_files()


@app.command(name="list-configs")
def list_configs() -> None:
    """List built-in configuration names."""
    for cfg in builtin_configurations():
        console.print(f"[bold]{cfg.name}[/bold]: {cfg.description}")


@contextmanager
def _build_harness(
    *,
    use_k8s: bool = True,
    k8s_namespace: str = "default",
) -> Iterator[Harness]:
    """Yield a Harness with the appropriate execution backend.

    Context-manager rather than a plain factory because the K8s
    backend provisions a real sandbox pod that *must* be terminated
    when the CLI command finishes — otherwise every ``--use-k8s``
    invocation leaks a pod (PR #6 review). The local-subprocess
    backend has no state so the no-op fast path is fine.

    ``use_k8s=True`` swaps :class:`LocalSubprocessBackend` for
    :class:`AgentSandboxBackend`, which expects a configured cluster
    plus the ``python`` SandboxTemplate installed; see
    ``docs/manual/k8s-sandbox.md`` for setup. ``k8s_namespace`` must
    match the namespace the template was applied to (``NAMESPACE=…
    make install-sandbox``); the backend default is ``"default"`` and
    a mismatch surfaces as ``SandboxTemplate "python" not found``.
    """
    if not use_k8s:
        yield Harness(catalog=Catalog.load_default(), runner=RealAgentRunner())
        return

    from harness_weaver.execution import AgentSandboxBackend

    with AgentSandboxBackend(namespace=k8s_namespace) as backend:
        yield Harness(
            catalog=Catalog.load_default(),
            runner=RealAgentRunner(),
            execution_backend=backend,
        )


def _write_trajectory(trajectory_json: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(trajectory_json, encoding="utf-8")
    console.print(f"trajectory written to [bold]{path}[/bold]")


def _resolve_config(name: str, model_override: str | None) -> "Configuration":
    """Look up a built-in configuration and apply an optional model override.

    The override is applied via pydantic's ``model_copy`` so the returned
    Configuration is a fresh frozen instance — built-ins stay immutable.
    """
    cfg = configuration_by_name(name)
    if model_override is None:
        return cfg
    return cfg.model_copy(update={"model": model_override})


_K8S_FLAG_HELP = (
    "Use the AgentSandboxBackend (kubernetes-sigs/agent-sandbox) for "
    "run_python instead of LocalSubprocessBackend. Enabled by default; "
    "pass --no-use-k8s to opt out. Requires a configured cluster and the "
    "'python' SandboxTemplate installed; see docs/manual/k8s-sandbox.md."
)

_K8S_NAMESPACE_HELP = (
    "Kubernetes namespace the 'python' SandboxTemplate was installed "
    "into. Must match the NAMESPACE you passed to 'make install-sandbox' "
    "(or to scripts/install-agent-sandbox.sh). Default 'default'."
)


@app.command()
def run(
    task: Annotated[Path, typer.Argument(help="Path to a task JSON file.", exists=True)],
    config: Annotated[
        str, typer.Option("--config", "-c", help="Configuration name to run.")
    ] = "single-agent-basic",
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help=(
                "Override the configuration's pinned model "
                "(e.g. 'claude-haiku-4-5-20251001'). Falls back to the "
                "Configuration's `model` field, then the SDK default."
            ),
        ),
    ] = None,
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="Directory for trajectory output.")
    ] = Path("runs"),
    use_k8s: Annotated[
        bool, typer.Option("--use-k8s/--no-use-k8s", help=_K8S_FLAG_HELP)
    ] = True,
    k8s_namespace: Annotated[
        str, typer.Option("--k8s-namespace", help=_K8S_NAMESPACE_HELP)
    ] = "default",
) -> None:
    """Run a single task with one configuration; emit a trajectory."""
    cfg = _resolve_config(config, model)
    task_obj = Task.from_path(task)
    with _build_harness(use_k8s=use_k8s, k8s_namespace=k8s_namespace) as harness:
        trajectory = harness.run(task_obj, cfg)
    out_path = output_dir / f"{trajectory.task_id}.{cfg.name}.json"
    _write_trajectory(trajectory.model_dump_json(indent=2), out_path)


@app.command()
def compare(
    task: Annotated[Path, typer.Argument(help="Path to a task JSON file.", exists=True)],
    config_a: Annotated[str, typer.Option("--config-a", help="Configuration A.")],
    config_b: Annotated[str, typer.Option("--config-b", help="Configuration B.")],
    model: Annotated[
        str | None,
        typer.Option("--model", help="Override the model for both configurations."),
    ] = None,
    judge_model: Annotated[
        str | None,
        typer.Option(
            "--judge-model",
            help=(
                "Run the LLM-as-judge with this Inspect-AI model id "
                "(e.g. 'anthropic/claude-haiku-4-5-20251001'). When set, "
                "writes a JSON verdict alongside the markdown report. "
                "Without this flag, only the rules-based structural "
                "report is produced — no API call."
            ),
        ),
    ] = None,
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="Directory for comparison output.")
    ] = Path("runs"),
    use_k8s: Annotated[
        bool, typer.Option("--use-k8s/--no-use-k8s", help=_K8S_FLAG_HELP)
    ] = True,
    k8s_namespace: Annotated[
        str, typer.Option("--k8s-namespace", help=_K8S_NAMESPACE_HELP)
    ] = "default",
) -> None:
    """Run the same task under two configurations and emit a side-by-side report.

    Two layers:

    * The structural report (rules-based, free) is always produced. It
      counts events, classifies failure modes, and pass/fails any
      ``Task.success_criteria``.
    * The LLM judge (paid, opt-in via ``--judge-model``) emits a
      JSON verdict that includes a winner, reasoning, and confidence.
    """
    import asyncio

    from harness_weaver.judge import StructuralReport, render_markdown
    from harness_weaver.judge.llm import InspectAILlmJudge

    cfg_a = _resolve_config(config_a, model)
    cfg_b = _resolve_config(config_b, model)
    task_obj = Task.from_path(task)
    trajectories = []
    with _build_harness(use_k8s=use_k8s, k8s_namespace=k8s_namespace) as harness:
        for cfg in (cfg_a, cfg_b):
            trajectory = harness.run(task_obj, cfg)
            out_path = output_dir / f"{trajectory.task_id}.{cfg.name}.json"
            _write_trajectory(trajectory.model_dump_json(indent=2), out_path)
            trajectories.append(trajectory)

    # Structural report: always run, no API.
    report = StructuralReport.of(trajectories[0], trajectories[1], task=task_obj)
    report_path = output_dir / f"{task_obj.task_id}.compare.md"
    report_path.write_text(render_markdown(report), encoding="utf-8")
    console.print(f"structural report written to [bold]{report_path}[/bold]")

    # LLM verdict: opt-in.
    if judge_model is not None:
        judge = InspectAILlmJudge(model=judge_model)
        verdict = asyncio.run(
            judge.verdict(
                task=task_obj,
                trajectory_a=trajectories[0],
                trajectory_b=trajectories[1],
            )
        )
        verdict_path = output_dir / f"{task_obj.task_id}.compare.verdict.json"
        verdict_path.write_text(verdict.model_dump_json(indent=2), encoding="utf-8")
        console.print(f"judge verdict written to [bold]{verdict_path}[/bold]")
        console.print(
            f"[bold]winner:[/bold] {verdict.winner}  "
            f"[dim](confidence {verdict.confidence:.2f})[/dim]"
        )


@app.command(name="eval")
def eval_(
    pack: Annotated[Path, typer.Argument(help="Path to a task pack JSON file.", exists=True)],
    config: Annotated[
        str, typer.Option("--config", "-c", help="Configuration name to evaluate.")
    ] = "single-agent-basic",
    model: Annotated[
        str | None,
        typer.Option("--model", help="Override the configuration's pinned model."),
    ] = None,
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="Directory for evaluation output.")
    ] = Path("runs"),
    use_k8s: Annotated[
        bool, typer.Option("--use-k8s/--no-use-k8s", help=_K8S_FLAG_HELP)
    ] = True,
    k8s_namespace: Annotated[
        str, typer.Option("--k8s-namespace", help=_K8S_NAMESPACE_HELP)
    ] = "default",
) -> None:
    """Evaluate one configuration against a full task pack.

    Per-task trajectories land in ``output_dir`` as
    ``{task_id}.{config_name}.json``; a pack-level markdown summary
    lands as ``{pack_name}.{config_name}.eval.md`` and aggregates
    completion rate, failure-mode frequencies, success-criteria pass
    rates, tool-call statistics, total duration, and total cost when
    the SDK reported it.
    """
    from harness_weaver.judge import PackSummary, render_pack_markdown

    cfg = _resolve_config(config, model)
    pack_obj = TaskPack.from_path(pack)
    trajectories = []
    with _build_harness(use_k8s=use_k8s, k8s_namespace=k8s_namespace) as harness:
        for task_obj in pack_obj.tasks:
            trajectory = harness.run(task_obj, cfg)
            out_path = output_dir / f"{trajectory.task_id}.{cfg.name}.json"
            _write_trajectory(trajectory.model_dump_json(indent=2), out_path)
            trajectories.append(trajectory)

    summary = PackSummary.of(trajectories, pack=pack_obj, configuration_name=cfg.name)
    summary_path = output_dir / f"{pack_obj.name}.{cfg.name}.eval.md"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(render_pack_markdown(summary), encoding="utf-8")
    console.print(f"pack summary written to [bold]{summary_path}[/bold]")
    console.print(
        f"[bold]completed:[/bold] {summary.completed_count}/{summary.task_count} "
        f"({summary.completion_rate:.0%})  "
        f"[dim]cost {summary.total_cost_usd if summary.total_cost_usd is not None else '-'}[/dim]"
    )


@app.command()
def serve(
    host: Annotated[
        str, typer.Option("--host", help="Address to bind. Default 127.0.0.1 (no auth!).")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="TCP port to listen on.")] = 8000,
    runs_dir: Annotated[
        Path,
        typer.Option(
            "--runs-dir",
            help="Directory the UI reads/writes trajectories and reports from.",
        ),
    ] = Path("runs"),
) -> None:
    """Start the optional web UI (requires ``pip install harness-weaver[web]``).

    Server-rendered pages for kicking off run / compare / eval and
    browsing trajectories. Single uvicorn worker, sync execution; the
    browser blocks for the duration of each run. Bind to 127.0.0.1 by
    default — there's no auth.
    """
    try:
        import uvicorn

        from harness_weaver.web import create_app
    except ImportError as exc:  # pragma: no cover - import-time error path
        console.print(
            "[red]The web UI requires extra dependencies. Install with:[/red] "
            "pip install -e '.[web]'"
        )
        raise typer.Exit(code=1) from exc

    web_app = create_app(runs_dir=runs_dir)
    console.print(f"harness-weaver web UI on http://{host}:{port}/")
    uvicorn.run(web_app, host=host, port=port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    app()

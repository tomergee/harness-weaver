"""Command-line interface for harness-weaver.

Three subcommands:

* ``run``     execute one task with one configuration; write the trajectory.
* ``compare`` run the same task under two configurations; write both
              trajectories. (Judge integration lands separately.)
* ``eval``    run a configuration over a task pack; write per-task
              trajectories. (Judge integration lands separately.)

The CLI wires the production stack: the bundled catalog, a fresh
:class:`LocalSubprocessBackend`, and :class:`RealAgentRunner`.
``RealAgentRunner`` is not implemented yet (see ``agent_runner.py``), so
the commands intentionally raise a clear ``NotImplementedError`` that
points at the next deliverable.

For programmatic use without the SDK wired up, instantiate
:class:`Harness` directly with a :class:`FakeAgentRunner`; this is the
path the e2e tests exercise.
"""

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from harness_weaver import __version__
from harness_weaver.agent_runner import RealAgentRunner
from harness_weaver.catalog import Catalog
from harness_weaver.configurations import builtin_configurations, configuration_by_name
from harness_weaver.harness import Harness
from harness_weaver.task import Task, TaskPack

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


@app.command(name="list-configs")
def list_configs() -> None:
    """List built-in configuration names."""
    for cfg in builtin_configurations():
        console.print(f"[bold]{cfg.name}[/bold]: {cfg.description}")


def _build_harness() -> Harness:
    return Harness(catalog=Catalog.load_default(), runner=RealAgentRunner())


def _write_trajectory(trajectory_json: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(trajectory_json, encoding="utf-8")
    console.print(f"trajectory written to [bold]{path}[/bold]")


@app.command()
def run(
    task: Annotated[Path, typer.Argument(help="Path to a task JSON file.", exists=True)],
    config: Annotated[
        str, typer.Option("--config", "-c", help="Configuration name to run.")
    ] = "single-agent-basic",
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="Directory for trajectory output.")
    ] = Path("runs"),
) -> None:
    """Run a single task with one configuration; emit a trajectory."""
    cfg = configuration_by_name(config)
    task_obj = Task.from_path(task)
    harness = _build_harness()
    trajectory = harness.run(task_obj, cfg)
    out_path = output_dir / f"{trajectory.task_id}.{cfg.name}.json"
    _write_trajectory(trajectory.model_dump_json(indent=2), out_path)


@app.command()
def compare(
    task: Annotated[Path, typer.Argument(help="Path to a task JSON file.", exists=True)],
    config_a: Annotated[str, typer.Option("--config-a", help="Configuration A.")],
    config_b: Annotated[str, typer.Option("--config-b", help="Configuration B.")],
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="Directory for comparison output.")
    ] = Path("runs"),
) -> None:
    """Run the same task under two configurations and emit a side-by-side report.

    The judge step (LLM-as-judge over the two trajectories) lands separately.
    For now this command writes both trajectories to disk so the judge can
    consume them later without re-running the agent.
    """
    cfg_a = configuration_by_name(config_a)
    cfg_b = configuration_by_name(config_b)
    task_obj = Task.from_path(task)
    harness = _build_harness()
    for cfg in (cfg_a, cfg_b):
        trajectory = harness.run(task_obj, cfg)
        out_path = output_dir / f"{trajectory.task_id}.{cfg.name}.json"
        _write_trajectory(trajectory.model_dump_json(indent=2), out_path)


@app.command(name="eval")
def eval_(
    pack: Annotated[Path, typer.Argument(help="Path to a task pack JSON file.", exists=True)],
    config: Annotated[
        str, typer.Option("--config", "-c", help="Configuration name to evaluate.")
    ] = "single-agent-basic",
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="Directory for evaluation output.")
    ] = Path("runs"),
) -> None:
    """Evaluate one configuration against a full task pack.

    Per-task trajectories are written to ``output_dir``. The aggregate
    judge report lands with the judge integration.
    """
    cfg = configuration_by_name(config)
    pack_obj = TaskPack.from_path(pack)
    harness = _build_harness()
    for task_obj in pack_obj.tasks:
        trajectory = harness.run(task_obj, cfg)
        out_path = output_dir / f"{trajectory.task_id}.{cfg.name}.json"
        _write_trajectory(trajectory.model_dump_json(indent=2), out_path)


if __name__ == "__main__":  # pragma: no cover
    app()

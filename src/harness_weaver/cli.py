"""Command-line interface for harness-weaver.

Three subcommands: ``run``, ``compare``, ``eval``. All currently stubs — the
implementations land in subsequent commits as the harness, configurations,
tools, and judge are built out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from harness_weaver import __version__

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
    version: Annotated[  # noqa: ARG001 — typer reads this via callback
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
    console.print(f"[yellow]Not yet implemented[/yellow]: run {task} with config={config}")
    raise typer.Exit(code=2)


@app.command()
def compare(
    task: Annotated[Path, typer.Argument(help="Path to a task JSON file.", exists=True)],
    config_a: Annotated[str, typer.Option("--config-a", help="Configuration A.")],
    config_b: Annotated[str, typer.Option("--config-b", help="Configuration B.")],
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="Directory for comparison output.")
    ] = Path("runs"),
) -> None:
    """Run the same task under two configurations and emit a side-by-side judge report."""
    console.print(
        f"[yellow]Not yet implemented[/yellow]: compare {task} "
        f"with {config_a} vs {config_b}"
    )
    raise typer.Exit(code=2)


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
    """Evaluate one configuration against a full task pack; emit a markdown report."""
    console.print(f"[yellow]Not yet implemented[/yellow]: eval {pack} with config={config}")
    raise typer.Exit(code=2)


if __name__ == "__main__":  # pragma: no cover
    app()

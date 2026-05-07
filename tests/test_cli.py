"""CLI smoke tests. Just enough to verify wiring; real behavior tests follow."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from harness_weaver import __version__
from harness_weaver.cli import app

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for subcommand in ("run", "compare", "eval"):
        assert subcommand in result.stdout


def test_run_stub_exits_with_code_2(tmp_path: Path, sample_task: dict[str, object]) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps(sample_task))
    result = runner.invoke(app, ["run", str(task_path), "--config", "single-agent-basic"])
    assert result.exit_code == 2
    assert "Not yet implemented" in result.stdout

"""CLI smoke tests.

Behavioral coverage of the CLI's success paths lives in
``test_harness_e2e.py`` (which exercises the Harness directly with a
:class:`FakeAgentRunner`). Here we verify the typer wiring: version flag,
help text, configuration listing, and that the production ``run`` command
fails with a clear message until the real SDK wiring lands.
"""

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
    for subcommand in ("run", "compare", "eval", "list-configs"):
        assert subcommand in result.stdout


def test_list_configs_prints_built_ins() -> None:
    result = runner.invoke(app, ["list-configs"])
    assert result.exit_code == 0
    for name in (
        "single-agent-basic",
        "single-agent-with-sandbox",
        "multi-agent-discovery-explainer",
    ):
        assert name in result.stdout


def test_run_unknown_config_fails_cleanly(tmp_path: Path, sample_task: dict[str, object]) -> None:
    task_path = tmp_path / "task.json"
    task_path.write_text(json.dumps(sample_task))
    result = runner.invoke(app, ["run", str(task_path), "--config", "not-a-real-config"])
    assert result.exit_code != 0
    assert isinstance(result.exception, KeyError)

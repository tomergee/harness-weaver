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

    import pytest

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


def test_model_override_propagates_via_resolve_config() -> None:
    """The CLI's --model flag should produce a Configuration with model set,
    leaving the original built-in untouched."""
    from harness_weaver.cli import _resolve_config
    from harness_weaver.configurations import SINGLE_AGENT_BASIC

    overridden = _resolve_config("single-agent-basic", "claude-haiku-4-5-20251001")
    assert overridden.model == "claude-haiku-4-5-20251001"
    # Built-in stays unmodified (frozen pydantic model).
    assert SINGLE_AGENT_BASIC.model is None


def test_model_none_keeps_configuration_default() -> None:
    from harness_weaver.cli import _resolve_config

    cfg = _resolve_config("single-agent-basic", None)
    assert cfg.model is None


def test_build_harness_context_manager_closes_k8s_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PR #6 review (HIGH): the K8s backend MUST be closed
    when the CLI command finishes — otherwise every ``--use-k8s``
    invocation leaks a sandbox pod. The ``_build_harness`` context
    manager owns that lifecycle.
    """
    from unittest.mock import MagicMock

    from harness_weaver.cli import _build_harness

    backend_instance = MagicMock()
    # AgentSandboxBackend supports `with`; mock the protocol explicitly.
    backend_instance.__enter__ = MagicMock(return_value=backend_instance)
    backend_instance.__exit__ = MagicMock(return_value=False)

    fake_class = MagicMock(return_value=backend_instance)
    monkeypatch.setattr("harness_weaver.execution.AgentSandboxBackend", fake_class)

    with _build_harness(use_k8s=True):
        pass

    backend_instance.__enter__.assert_called_once()
    backend_instance.__exit__.assert_called_once()


def test_build_harness_threads_k8s_namespace_to_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for PR #8 review (MED): if the user installed the
    SandboxTemplate to a non-default namespace via ``NAMESPACE=harness
    make install-sandbox``, the CLI ``--k8s-namespace harness`` flag must
    flow through to ``AgentSandboxBackend(namespace=...)``. Without this
    wiring the backend silently defaults to ``"default"`` and the run
    fails with ``SandboxTemplate "python" not found``.
    """
    from unittest.mock import MagicMock

    from harness_weaver.cli import _build_harness

    backend_instance = MagicMock()
    backend_instance.__enter__ = MagicMock(return_value=backend_instance)
    backend_instance.__exit__ = MagicMock(return_value=False)

    fake_class = MagicMock(return_value=backend_instance)
    monkeypatch.setattr("harness_weaver.execution.AgentSandboxBackend", fake_class)

    with _build_harness(use_k8s=True, k8s_namespace="harness"):
        pass

    fake_class.assert_called_once_with(namespace="harness")


def test_build_harness_no_backend_when_local() -> None:
    """When ``use_k8s=False``, no K8s backend is constructed at all —
    avoids the import cost and any cluster connection attempt."""
    from harness_weaver.cli import _build_harness

    with _build_harness(use_k8s=False) as harness:
        # Local default: backend was constructed by Harness internally.
        # We just verify no exception and that we got a Harness back.
        assert harness is not None


def test_build_harness_defaults_to_k8s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default path should provision the K8s backend unless explicitly disabled."""
    from unittest.mock import MagicMock

    from harness_weaver.cli import _build_harness

    backend_instance = MagicMock()
    backend_instance.__enter__ = MagicMock(return_value=backend_instance)
    backend_instance.__exit__ = MagicMock(return_value=False)

    fake_class = MagicMock(return_value=backend_instance)
    monkeypatch.setattr("harness_weaver.execution.AgentSandboxBackend", fake_class)

    with _build_harness():
        pass

    fake_class.assert_called_once_with(namespace="default")


# --- _warn_if_k8s_noop ----------------------------------------------------


class TestK8sNoopWarning:
    """The warning the CLI prints when --use-k8s is set but no chosen
    configuration's agents expose run_python. Without this, the flag
    silently does nothing — the AgentSandboxBackend is wired in but
    never reached, and the user wonders why no pod ever shows up.
    """

    def test_warns_for_single_agent_basic(self) -> None:
        from io import StringIO

        from rich.console import Console as _Console

        import harness_weaver.cli as cli_mod
        from harness_weaver.cli import _warn_if_k8s_noop
        from harness_weaver.configurations import SINGLE_AGENT_BASIC

        buf = StringIO()
        original = cli_mod.console
        cli_mod.console = _Console(file=buf, force_terminal=False, width=120)
        try:
            _warn_if_k8s_noop(True, SINGLE_AGENT_BASIC)
        finally:
            cli_mod.console = original

        out = buf.getvalue()
        assert "warning" in out.lower()
        assert "run_python" in out
        assert "single-agent-basic" in out
        assert "single-agent-with-sandbox" in out  # the suggested fix

    def test_no_warning_for_multi_agent_discovery_explainer(self) -> None:
        """The multi-agent built-in's discovery worker has run_python in
        its allow-list — ``uses_run_python`` walks the union of all
        agents, so the warning is silent. (The agent might still choose
        not to call it on a given task, but that's a runtime concern,
        not a configuration concern.)
        """
        from io import StringIO

        from rich.console import Console as _Console

        import harness_weaver.cli as cli_mod
        from harness_weaver.cli import _warn_if_k8s_noop
        from harness_weaver.configurations import MULTI_AGENT_DISCOVERY_EXPLAINER

        buf = StringIO()
        original = cli_mod.console
        cli_mod.console = _Console(file=buf, force_terminal=False, width=120)
        try:
            _warn_if_k8s_noop(True, MULTI_AGENT_DISCOVERY_EXPLAINER)
        finally:
            cli_mod.console = original

        assert buf.getvalue() == ""

    def test_quiet_when_use_k8s_false(self) -> None:
        from io import StringIO

        from rich.console import Console as _Console

        import harness_weaver.cli as cli_mod
        from harness_weaver.cli import _warn_if_k8s_noop
        from harness_weaver.configurations import SINGLE_AGENT_BASIC

        buf = StringIO()
        original = cli_mod.console
        cli_mod.console = _Console(file=buf, force_terminal=False, width=120)
        try:
            _warn_if_k8s_noop(False, SINGLE_AGENT_BASIC)
        finally:
            cli_mod.console = original

        assert buf.getvalue() == ""

    def test_quiet_when_a_config_uses_run_python(self) -> None:
        """Compare-style: as long as *one* of the configs hits the
        sandbox, the flag isn't a no-op — no warning."""
        from io import StringIO

        from rich.console import Console as _Console

        import harness_weaver.cli as cli_mod
        from harness_weaver.cli import _warn_if_k8s_noop
        from harness_weaver.configurations import (
            SINGLE_AGENT_BASIC,
            SINGLE_AGENT_WITH_SANDBOX,
        )

        buf = StringIO()
        original = cli_mod.console
        cli_mod.console = _Console(file=buf, force_terminal=False, width=120)
        try:
            _warn_if_k8s_noop(True, SINGLE_AGENT_BASIC, SINGLE_AGENT_WITH_SANDBOX)
        finally:
            cli_mod.console = original

        assert buf.getvalue() == ""

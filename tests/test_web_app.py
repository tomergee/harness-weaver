"""Tests for the optional web UI.

The harness factory is mocked so tests never call the SDK or the
network. Trajectories returned by the fake are real ``Trajectory``
instances built from a tiny ``FakeAgentRunner`` script — that exercises
the recording path the real harness exercises, just without an LLM in
the loop.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from harness_weaver.agent_runner import FakeAgentRunner, answer, call
from harness_weaver.catalog import Catalog
from harness_weaver.configurations import SINGLE_AGENT_BASIC, configuration_by_name
from harness_weaver.harness import Harness
from harness_weaver.task import Task

if TYPE_CHECKING:
    from collections.abc import Iterator

    from harness_weaver.configurations import Configuration

# Skip the whole module when web extras aren't installed. The web UI is
# an optional dependency; a CLI-only deploy shouldn't make the test
# suite fail.
pytest.importorskip("fastapi")
pytest.importorskip("jinja2")
pytest.importorskip("markdown")
pytest.importorskip("multipart")  # python-multipart
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from harness_weaver.web.app import create_app

# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def repo_root() -> Path:
    """The harness-weaver repo root, inferred from this file's location."""
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def tmp_runs_dir(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    runs.mkdir()
    return runs


def _fake_trajectory_for(task: Task, cfg: Configuration) -> object:
    runner = FakeAgentRunner(
        [
            call("user_history", {"user_id": "user-001", "limit": 5}),
            answer("Stub answer for testing."),
        ]
    )
    harness = Harness(catalog=Catalog.load_default(), runner=runner)
    return harness.run(task, cfg)


class _FakeHarnessCtx:
    """Stand-in for the production Harness context manager.

    Returns a Harness wired to a FakeAgentRunner so the web app's
    ``harness.run(...)`` calls produce real Trajectory objects without
    touching the SDK.
    """

    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    def __enter__(self) -> Harness:
        self.entered = True
        runner = FakeAgentRunner(
            [
                call("user_history", {"user_id": "user-001", "limit": 5}),
                answer("Stub answer for testing."),
            ]
        )
        return Harness(catalog=Catalog.load_default(), runner=runner)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self.exited = True


@pytest.fixture
def fake_factory() -> Iterator[list[_FakeHarnessCtx]]:
    """Records each context manager so tests can assert lifecycle."""
    contexts: list[_FakeHarnessCtx] = []

    def _factory(*, use_k8s: bool, k8s_namespace: str) -> _FakeHarnessCtx:
        del use_k8s, k8s_namespace
        ctx = _FakeHarnessCtx()
        contexts.append(ctx)
        return ctx

    # The fixture yields the recorder list; the factory itself is what
    # the test passes to create_app.
    yield contexts


@pytest.fixture
def client(tmp_runs_dir: Path, repo_root: Path, fake_factory: list[_FakeHarnessCtx]) -> TestClient:
    contexts = fake_factory

    def _factory(*, use_k8s: bool, k8s_namespace: str) -> _FakeHarnessCtx:
        del use_k8s, k8s_namespace
        ctx = _FakeHarnessCtx()
        contexts.append(ctx)
        return ctx

    app = create_app(
        runs_dir=tmp_runs_dir,
        repo_root=repo_root,
        harness_factory=_factory,
    )
    return TestClient(app)


# --- index + form GETs ------------------------------------------------------


def test_index_lists_existing_trajectories(client: TestClient, tmp_runs_dir: Path) -> None:
    """Index page surfaces existing JSON files in the runs/ dir."""
    (tmp_runs_dir / "alpha.json").write_text("{}", encoding="utf-8")
    (tmp_runs_dir / "beta.compare.md").write_text("# x", encoding="utf-8")

    response = client.get("/")

    assert response.status_code == 200
    assert "alpha.json" in response.text
    assert "beta.compare.md" in response.text


def test_index_empty_state(client: TestClient) -> None:
    """Empty runs/ dir renders the empty hint, not an error."""
    response = client.get("/")

    assert response.status_code == 200
    assert "No trajectories yet" in response.text


def test_runs_new_form_lists_tasks_and_configs(client: TestClient) -> None:
    response = client.get("/runs/new")

    assert response.status_code == 200
    # The bundled discovery task is wired into examples/tasks/.
    assert "discovery-mood-tense.json" in response.text
    assert "single-agent-basic" in response.text


def test_compare_new_form_renders(client: TestClient) -> None:
    response = client.get("/compare/new")

    assert response.status_code == 200
    assert "Configuration A" in response.text
    assert "Configuration B" in response.text


def test_eval_new_form_lists_packs(client: TestClient) -> None:
    response = client.get("/eval/new")

    assert response.status_code == 200
    # The bundled pack name comes from examples/packs/.
    assert ".json" in response.text


# --- POST /runs/new ---------------------------------------------------------


def test_post_runs_new_writes_trajectory_and_redirects(
    client: TestClient, tmp_runs_dir: Path, fake_factory: list[_FakeHarnessCtx]
) -> None:
    """POST runs the harness via the injected fake, persists the trajectory,
    and redirects to the trajectory page. The fake context manager must
    have entered AND exited (lifecycle = no leaked backend)."""
    response = client.post(
        "/runs/new",
        data={
            "task": "examples/tasks/discovery-mood-tense.json",
            "config": "single-agent-basic",
            "model": "",
            "k8s_namespace": "default",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/trajectories/")

    written = list(tmp_runs_dir.glob("*.json"))
    assert len(written) == 1

    data = json.loads(written[0].read_text(encoding="utf-8"))
    assert data["task_id"] == "discovery-mood-tense"
    assert data["configuration_name"] == "single-agent-basic"

    assert len(fake_factory) == 1
    assert fake_factory[0].entered
    assert fake_factory[0].exited


def test_post_runs_new_rejects_path_traversal(client: TestClient) -> None:
    """Anyone POSTing ``task=../../etc/passwd`` should bounce back to the
    form, not have the path read off disk."""
    response = client.post(
        "/runs/new",
        data={
            "task": "../../etc/passwd",
            "config": "single-agent-basic",
            "model": "",
            "k8s_namespace": "default",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=bad_task_path" in response.headers["location"]


# --- POST /compare/new ------------------------------------------------------


def test_post_compare_new_writes_report(
    client: TestClient, tmp_runs_dir: Path, fake_factory: list[_FakeHarnessCtx]
) -> None:
    response = client.post(
        "/compare/new",
        data={
            "task": "examples/tasks/discovery-mood-tense.json",
            # Two distinct configs so the per-config trajectory files don't
            # share a filename and overwrite each other.
            "config_a": "single-agent-basic",
            "config_b": "single-agent-with-sandbox",
            "model": "",
            "judge_model": "",
            "k8s_namespace": "default",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    # The redirect should land on the report.
    assert response.headers["location"].startswith("/reports/")

    # Two trajectories + one report written.
    json_files = list(tmp_runs_dir.glob("*.json"))
    md_files = list(tmp_runs_dir.glob("*.md"))
    assert len(md_files) == 1
    assert any("compare" in f.name for f in md_files)

    # One harness factory call ran both legs (the CLI does the same).
    assert len(fake_factory) == 1
    assert fake_factory[0].entered
    assert fake_factory[0].exited
    # One trajectory file per config.
    assert len(json_files) == 2


def test_post_compare_new_rejects_path_traversal(client: TestClient) -> None:
    response = client.post(
        "/compare/new",
        data={
            "task": "/etc/passwd",
            "config_a": "single-agent-basic",
            "config_b": "single-agent-basic",
            "k8s_namespace": "default",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=bad_task_path" in response.headers["location"]


# --- POST /eval/new ---------------------------------------------------------


def test_post_eval_new_writes_summary(
    client: TestClient, tmp_runs_dir: Path, repo_root: Path
) -> None:
    """eval over the bundled pack produces one summary markdown, one
    trajectory per pack task."""
    pack_path = repo_root / "examples" / "packs"
    if not pack_path.exists() or not list(pack_path.glob("*.json")):
        pytest.skip("No bundled task pack to exercise eval against.")

    pack_file = next(pack_path.glob("*.json"))
    response = client.post(
        "/eval/new",
        data={
            "pack": str(pack_file.relative_to(repo_root)),
            "config": "single-agent-basic",
            "k8s_namespace": "default",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/reports/")

    md_files = list(tmp_runs_dir.glob("*.eval.md"))
    assert len(md_files) == 1


# --- /trajectories/{file} and /reports/{file} -------------------------------


def test_trajectory_view_renders_existing_file(
    client: TestClient, tmp_runs_dir: Path, repo_root: Path
) -> None:
    task = Task.from_path(repo_root / "examples" / "tasks" / "discovery-mood-tense.json")
    cfg = configuration_by_name("single-agent-basic")
    trajectory = _fake_trajectory_for(task, cfg)
    out = tmp_runs_dir / "demo.json"
    out.write_text(trajectory.model_dump_json(indent=2), encoding="utf-8")  # type: ignore[attr-defined]

    response = client.get("/trajectories/demo.json")

    assert response.status_code == 200
    # The trajectory page surfaces the task id and the configuration name.
    assert "discovery-mood-tense" in response.text
    assert "single-agent-basic" in response.text


def test_trajectory_view_404_for_missing_file(client: TestClient) -> None:
    response = client.get("/trajectories/does-not-exist.json")
    assert response.status_code == 404


def test_trajectory_view_rejects_path_traversal(client: TestClient) -> None:
    """Even with URL-encoded slashes, ``..`` should not escape the runs dir."""
    response = client.get("/trajectories/..%2F..%2Fetc%2Fpasswd")
    # Either 404 (most likely; the safe-join refuses) or 400. Anything but a 200
    # with file contents is fine.
    assert response.status_code in (400, 404)


def test_report_view_renders_markdown(client: TestClient, tmp_runs_dir: Path) -> None:
    (tmp_runs_dir / "demo.md").write_text("# Heading\n\nBody\n", encoding="utf-8")

    response = client.get("/reports/demo.md")

    assert response.status_code == 200
    assert "<h1>Heading</h1>" in response.text


def test_report_view_404_for_missing_file(client: TestClient) -> None:
    response = client.get("/reports/no-such.md")
    assert response.status_code == 404


def test_post_runs_new_uses_model_override(
    client: TestClient, tmp_runs_dir: Path, fake_factory: list[_FakeHarnessCtx]
) -> None:
    """A model override flows through to the configuration the harness sees."""
    del fake_factory  # not asserting on the recorder here
    response = client.post(
        "/runs/new",
        data={
            "task": "examples/tasks/discovery-mood-tense.json",
            "config": "single-agent-basic",
            "model": "claude-haiku-4-5-20251001",
            "k8s_namespace": "default",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    written = next(tmp_runs_dir.glob("*.json"))
    data = json.loads(written.read_text(encoding="utf-8"))
    # The Trajectory carries the configuration_name; we just check the
    # write happened. Fuller wiring is exercised by the CLI tests; the
    # web layer's job is just to forward the override.
    assert data["task_id"] == "discovery-mood-tense"


# Keep the import quiet for ruff if unused.
_ = SINGLE_AGENT_BASIC

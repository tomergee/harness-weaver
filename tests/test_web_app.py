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
    assert 'name="use_k8s" value="true" checked' in response.text


def test_compare_new_form_renders(client: TestClient) -> None:
    response = client.get("/compare/new")

    assert response.status_code == 200
    assert "Configuration A" in response.text
    assert "Configuration B" in response.text
    assert 'name="use_k8s" value="true" checked' in response.text


def test_eval_new_form_lists_packs(client: TestClient) -> None:
    response = client.get("/eval/new")

    assert response.status_code == 200
    # The bundled pack name comes from examples/packs/.
    assert ".json" in response.text
    assert 'name="use_k8s" value="true" checked' in response.text


# --- POST /runs/new ---------------------------------------------------------


def _wait_for_job_done(client: TestClient, job_id: str, timeout_s: float = 5.0) -> dict:
    """Poll /jobs/{id}.json until the job reaches a terminal status.

    Form POSTs now enqueue jobs on a worker thread (see
    :mod:`harness_weaver.web.jobs`) and 303 to /jobs/{id}. Tests use
    this helper to wait for completion before asserting on artifacts.
    """
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        response = client.get(f"/jobs/{job_id}.json")
        assert response.status_code == 200, response.text
        snap = response.json()
        if snap["status"] in {"done", "error"}:
            return snap
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout_s}s")


def _submit_and_wait(client: TestClient, url: str, data: dict, timeout_s: float = 10.0) -> dict:
    response = client.post(url, data=data, follow_redirects=False)
    assert response.status_code == 303, (response.status_code, response.text)
    location = response.headers["location"]
    assert location.startswith("/jobs/"), f"expected job redirect, got {location}"
    job_id = location.removeprefix("/jobs/")
    return _wait_for_job_done(client, job_id, timeout_s=timeout_s)


def test_post_runs_new_enqueues_job_and_writes_trajectory(
    client: TestClient, tmp_runs_dir: Path, fake_factory: list[_FakeHarnessCtx]
) -> None:
    """POST enqueues a job, redirects to /jobs/{id}; the worker walks
    through phases (load-task → resolve-config → build-harness →
    sdk-call → write-output) and writes the trajectory.

    The fake harness context must have entered AND exited (lifecycle
    = no leaked backend) — same invariant the blocking flow had.
    """
    snap = _submit_and_wait(
        client,
        "/runs/new",
        {
            "task": "examples/tasks/discovery-mood-tense.json",
            "config": "single-agent-basic",
            "model": "",
            "k8s_namespace": "default",
        },
    )

    assert snap["status"] == "done", snap
    assert snap["redirect_url"].startswith("/trajectories/")
    # All planned steps should be in 'done' state.
    assert all(s["status"] == "done" for s in snap["steps"]), [
        (s["id"], s["status"]) for s in snap["steps"]
    ]

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
    snap = _submit_and_wait(
        client,
        "/compare/new",
        {
            # Same config name on both legs — exercises the per-leg
            # filename suffix (PR #11 review #3). Without the suffix the
            # second trajectory would overwrite the first.
            "task": "examples/tasks/discovery-mood-tense.json",
            "config_a": "single-agent-basic",
            "config_b": "single-agent-basic",
            "model": "",
            "judge_model": "",
            "k8s_namespace": "default",
        },
    )

    assert snap["status"] == "done", snap
    assert snap["redirect_url"].startswith("/reports/")

    # Two trajectories + one report written. With the .0/.1 suffix even
    # same-config compare keeps both legs on disk.
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
    snap = _submit_and_wait(
        client,
        "/eval/new",
        {
            "pack": str(pack_file.relative_to(repo_root)),
            "config": "single-agent-basic",
            "k8s_namespace": "default",
        },
        timeout_s=20.0,
    )

    assert snap["status"] == "done", snap
    assert snap["redirect_url"].startswith("/reports/")

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


def test_report_view_sanitizes_html(client: TestClient, tmp_runs_dir: Path) -> None:
    """PR #11 review: markdown content can carry LLM output which we
    treat as untrusted. <script> and inline event handlers must be
    stripped before reaching the browser.
    """
    body = (
        "# Title\n\n"
        "<script>alert('xss')</script>\n\n"
        "<img src=x onerror=alert('xss')>\n\n"
        "Plain text with [a link](https://example.com).\n"
    )
    (tmp_runs_dir / "evil.md").write_text(body, encoding="utf-8")

    response = client.get("/reports/evil.md")

    assert response.status_code == 200
    # Allowed content survives.
    assert "<h1>Title</h1>" in response.text
    assert 'href="https://example.com"' in response.text
    # Executable surfaces are gone. Bleach's strip mode removes the
    # <script> tag and the onerror attribute; any leftover text content
    # is harmless because the browser only renders it as text. We assert
    # on the dangerous *attributes* and *tags*, not on the text body.
    assert "<script" not in response.text
    assert "</script" not in response.text
    assert "onerror" not in response.text


def test_report_view_surfaces_verdict_when_present(client: TestClient, tmp_runs_dir: Path) -> None:
    """PR #11 review: when --judge-model wrote a verdict alongside a
    compare report, the report page must surface it. Otherwise the
    user paid for a verdict they never see.
    """
    (tmp_runs_dir / "alpha.compare.md").write_text("# Compare\n", encoding="utf-8")
    (tmp_runs_dir / "alpha.compare.verdict.json").write_text(
        json.dumps(
            {
                "winner": "a",
                "confidence": 0.78,
                "reasoning": "Distinctive verdict reasoning text.",
            }
        ),
        encoding="utf-8",
    )

    response = client.get("/reports/alpha.compare.md")

    assert response.status_code == 200
    assert "Judge verdict" in response.text
    assert "Distinctive verdict reasoning text." in response.text
    assert "0.78" in response.text


def test_report_view_no_verdict_section_when_absent(client: TestClient, tmp_runs_dir: Path) -> None:
    """A compare report without a sibling verdict file should not
    render the verdict section at all (no empty 'winner: None' UI)."""
    (tmp_runs_dir / "beta.compare.md").write_text("# Compare\n", encoding="utf-8")

    response = client.get("/reports/beta.compare.md")

    assert response.status_code == 200
    assert "Judge verdict" not in response.text


def test_runs_new_form_renders_error_message(client: TestClient) -> None:
    """PR #11 review: the redirect carries ``error=bad_task_path`` but
    the form has to actually display it for the user to know what
    went wrong.
    """
    response = client.get("/runs/new?error=bad_task_path")

    assert response.status_code == 200
    assert "Task path must be inside the repository" in response.text


def test_post_runs_new_uses_model_override(
    client: TestClient, tmp_runs_dir: Path, fake_factory: list[_FakeHarnessCtx]
) -> None:
    """A model override flows through to the configuration the harness sees."""
    del fake_factory  # not asserting on the recorder here
    snap = _submit_and_wait(
        client,
        "/runs/new",
        {
            "task": "examples/tasks/discovery-mood-tense.json",
            "config": "single-agent-basic",
            "model": "claude-haiku-4-5-20251001",
            "k8s_namespace": "default",
        },
    )
    assert snap["status"] == "done"
    written = next(tmp_runs_dir.glob("*.json"))
    data = json.loads(written.read_text(encoding="utf-8"))
    # The Trajectory carries the configuration_name; we just check the
    # write happened. Fuller wiring is exercised by the CLI tests; the
    # web layer's job is just to forward the override.
    assert data["task_id"] == "discovery-mood-tense"


def test_post_runs_new_unchecked_k8s_disables_backend(
    tmp_runs_dir: Path,
    repo_root: Path,
) -> None:
    """Unchecked checkbox omits form key; backend selection must become False."""
    seen: dict[str, object] = {}

    class _RecordingHarnessCtx(_FakeHarnessCtx):
        pass

    def _factory(*, use_k8s: bool, k8s_namespace: str) -> _RecordingHarnessCtx:
        seen["use_k8s"] = use_k8s
        seen["k8s_namespace"] = k8s_namespace
        return _RecordingHarnessCtx()

    app = create_app(
        runs_dir=tmp_runs_dir,
        repo_root=repo_root,
        harness_factory=_factory,
    )
    client = TestClient(app)

    # Simulate browser submit with checkbox unchecked: no `use_k8s` key present.
    snap = _submit_and_wait(
        client,
        "/runs/new",
        {
            "task": "examples/tasks/discovery-mood-tense.json",
            "config": "single-agent-basic",
            "model": "",
            "k8s_namespace": "default",
        },
    )
    assert snap["status"] == "done", snap
    assert seen["use_k8s"] is False


# --- Job page + SSE --------------------------------------------------------


def test_job_page_renders_steps_and_metadata(
    client: TestClient, fake_factory: list[_FakeHarnessCtx]
) -> None:
    """The /jobs/{id} page shows the job title, the planned step list,
    and an empty event log placeholder before the worker has finished.
    """
    del fake_factory
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
    job_id = response.headers["location"].removeprefix("/jobs/")

    page = client.get(f"/jobs/{job_id}")
    assert page.status_code == 200
    # Title + planned steps + timer + log container all rendered.
    assert "discovery-mood-tense" in page.text
    assert "Run agent loop" in page.text
    assert "Persist trajectory" in page.text
    assert 'id="job-timer"' in page.text
    assert 'id="event-log"' in page.text


def test_job_json_endpoint_reflects_completed_run(
    client: TestClient, fake_factory: list[_FakeHarnessCtx]
) -> None:
    """The /jobs/{id}.json endpoint returns the same snapshot the SSE
    'done' event ships, with all steps marked done."""
    del fake_factory
    snap = _submit_and_wait(
        client,
        "/runs/new",
        {
            "task": "examples/tasks/discovery-mood-tense.json",
            "config": "single-agent-basic",
            "model": "",
            "k8s_namespace": "default",
        },
    )
    assert snap["status"] == "done"
    # Steps cover the documented run-job phases.
    step_ids = {s["id"] for s in snap["steps"]}
    assert step_ids == {
        "load-task",
        "resolve-config",
        "build-harness",
        "sdk-call",
        "write-output",
    }
    # All steps end in 'done' status.
    assert all(s["status"] == "done" for s in snap["steps"])
    # Per-phase events landed in the events list, including step+detail.
    events = snap["events"]
    assert any(e["step"] == "sdk-call" and e["status"] == "running" for e in events)
    assert any(e["step"] == "write-output" and e["status"] == "done" for e in events)


def test_sse_endpoint_streams_until_done(
    client: TestClient, fake_factory: list[_FakeHarnessCtx]
) -> None:
    """/jobs/{id}/events streams phase events and closes with an
    ``event: done`` marker carrying the full snapshot."""
    del fake_factory
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
    job_id = response.headers["location"].removeprefix("/jobs/")

    sse = client.get(f"/jobs/{job_id}/events")
    assert sse.status_code == 200
    assert sse.headers["content-type"].startswith("text/event-stream")
    body = sse.text
    # At least one phase event arrived as a data: line.
    assert "data: " in body
    # The terminal 'event: done' marker fires.
    assert "event: done" in body
    # The terminal payload carries the redirect URL.
    assert "/trajectories/" in body


def test_job_view_404_for_unknown_id(client: TestClient) -> None:
    response = client.get("/jobs/does-not-exist")
    assert response.status_code == 404


def test_form_renders_config_summaries_for_sidebar(client: TestClient) -> None:
    """Form pages embed all config summaries as JSON for the JS sidebar."""
    response = client.get("/runs/new")
    assert response.status_code == 200
    assert 'id="config-summaries-data"' in response.text
    assert 'id="config-explainer"' in response.text
    # Embedded JSON should at minimum include each built-in config name.
    assert "single-agent-basic" in response.text
    assert "multi-agent-discovery-explainer" in response.text


# Keep the import quiet for ruff if unused.
_ = SINGLE_AGENT_BASIC


# --- build-harness step detail (K8s no-op surfacing) ----------------------


def test_build_harness_step_says_no_op_for_basic_config(
    client: TestClient, fake_factory: list[_FakeHarnessCtx]
) -> None:
    """The build-harness step on the live job page must call out that
    --use-k8s does nothing for single-agent-basic (no run_python in
    any agent's allowed-tools). User-visible UX bug otherwise."""
    del fake_factory
    snap = _submit_and_wait(
        client,
        "/runs/new",
        {
            "task": "examples/tasks/discovery-mood-tense.json",
            "config": "single-agent-basic",
            "model": "",
            "use_k8s": "true",
            "k8s_namespace": "default",
        },
    )
    detail = next(
        e["detail"]
        for e in snap["events"]
        if e["step"] == "build-harness" and e["status"] == "done"
    )
    assert "AgentSandboxBackend" in detail
    assert "no agent" in detail
    assert "single-agent-basic" in detail
    assert "no-op" in detail


def test_build_harness_step_says_pod_will_be_provisioned_for_sandbox_config(
    client: TestClient, fake_factory: list[_FakeHarnessCtx]
) -> None:
    """And the inverse: when the config DOES expose run_python, the
    step says a pod will be provisioned."""
    del fake_factory
    snap = _submit_and_wait(
        client,
        "/runs/new",
        {
            "task": "examples/tasks/discovery-mood-tense.json",
            "config": "single-agent-with-sandbox",
            "model": "",
            "use_k8s": "true",
            "k8s_namespace": "default",
        },
    )
    detail = next(
        e["detail"]
        for e in snap["events"]
        if e["step"] == "build-harness" and e["status"] == "done"
    )
    assert "AgentSandboxBackend" in detail
    assert "pod will be provisioned" in detail


def test_build_harness_step_says_local_when_use_k8s_false(
    client: TestClient, fake_factory: list[_FakeHarnessCtx]
) -> None:
    del fake_factory
    snap = _submit_and_wait(
        client,
        "/runs/new",
        {
            "task": "examples/tasks/discovery-mood-tense.json",
            "config": "single-agent-with-sandbox",
            "model": "",
            # use_k8s omitted from the form — checkbox-unchecked semantics.
            "k8s_namespace": "default",
        },
    )
    detail = next(
        e["detail"]
        for e in snap["events"]
        if e["step"] == "build-harness" and e["status"] == "done"
    )
    assert "LocalSubprocessBackend" in detail

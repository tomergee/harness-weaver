"""Job system for the web UI's long-running run/compare/eval submissions.

The plain web UI made the browser block for the duration of every harness
call (20-90s for a single run, several minutes for a pack). The user
asked for something reactive and explanatory: live phase events, an
elapsed-time counter, a step-by-step view of what the harness is doing.

This module implements the server side. The HTTP layer in :mod:`.app`
calls :meth:`JobRegistry.submit` to enqueue a job, returns a 303
redirect to ``/jobs/{id}``, and exposes ``/jobs/{id}/events`` as a
Server-Sent Events stream. The browser opens an EventSource on that
URL, ticks a timer, and updates the step list as phase events arrive.

Threading model: jobs run on a single-worker
:class:`concurrent.futures.ThreadPoolExecutor` so the harness's sync
API stays sync. The worker thread appends to ``Job.events`` (guarded
by a lock); the SSE endpoint polls ``Job.events`` from the asyncio
loop on a 200 ms cadence and ships any new entries to the browser.
Polling is lower-tech than a cross-thread asyncio.Queue but avoids
the loop-shoehorning required to push from a worker thread, and the
latency is fine for human-perceptible progress.

Jobs are kept in memory; restart the server and they're gone. The
corresponding *artifacts* (trajectory JSON, report MD, verdict JSON)
land on disk under ``runs_dir`` like the CLI writes them, so the
browse pages still work after a restart — only the live job state
is volatile.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from harness_weaver.configurations import Configuration, configuration_by_name
from harness_weaver.task import Task, TaskPack

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from harness_weaver.harness import Harness
    from harness_weaver.trajectory import Trajectory


JobType = Literal["run", "compare", "eval"]
JobStatus = Literal["queued", "running", "done", "error"]
StepStatus = Literal["pending", "running", "done", "error"]


@dataclass
class JobEvent:
    """A single phase-state transition for a job.

    ``step`` is a stable machine identifier (``load-task``, ``sdk-call``,
    etc.) the front-end uses to find the right row in the step list.
    ``status`` is its new state. ``detail`` is human-readable copy that
    gets appended/replaced under the step.
    """

    step: str
    status: StepStatus
    detail: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "status": self.status,
            "detail": self.detail,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class JobStep:
    """A planned step the front-end can render even before it has fired.

    The job page paints all steps at the start (greyed out), then the SSE
    stream walks them through pending → running → done as they happen.
    ``id`` matches :class:`JobEvent.step`.
    """

    id: str
    label: str
    description: str
    status: StepStatus = "pending"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "status": self.status,
        }


@dataclass
class Job:
    """A unit of work submitted from a web form.

    All fields are read by the SSE poller; the worker thread writes
    them under :attr:`_lock`. The lock is RLock so the worker can call
    :meth:`emit` re-entrantly inside step helpers.
    """

    id: str
    job_type: JobType
    title: str
    params: dict[str, Any]
    steps: list[JobStep]
    status: JobStatus = "queued"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    redirect_url: str | None = None
    error: str | None = None
    events: list[JobEvent] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def emit(self, step: str, status: StepStatus, detail: str = "") -> None:
        """Append a phase event and propagate to the matching step row."""
        event = JobEvent(step=step, status=status, detail=detail)
        with self._lock:
            self.events.append(event)
            for s in self.steps:
                if s.id == step:
                    s.status = status
                    break

    def snapshot(self) -> dict[str, Any]:
        """Pickle-free dict for JSON responses and the initial template render."""
        with self._lock:
            return {
                "id": self.id,
                "job_type": self.job_type,
                "title": self.title,
                "params": self.params,
                "steps": [s.to_dict() for s in self.steps],
                "status": self.status,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
                "redirect_url": self.redirect_url,
                "error": self.error,
                "events": [e.to_dict() for e in self.events],
            }

    def is_terminal(self) -> bool:
        return self.status in {"done", "error"}


# Step plans -- pre-baked descriptions per job type. The job page shows
# these greyed out at the start and the SSE stream lights them up in
# order. Keeping the copy here (vs. the template) means the same source
# of truth feeds both the initial render and any future textual report.

_RUN_STEPS: tuple[JobStep, ...] = (
    JobStep(
        "load-task",
        "Load task",
        "Read the task JSON, validate it as a pydantic Task model, surface the user prompt.",
    ),
    JobStep(
        "resolve-config",
        "Resolve configuration",
        (
            "Look up the configuration by name, apply the optional model override "
            "via Configuration.model_copy. The frozen built-in stays untouched."
        ),
    ),
    JobStep(
        "build-harness",
        "Build harness",
        (
            "Construct the Harness with the catalog, the RealAgentRunner, and the "
            "selected execution backend (LocalSubprocessBackend or, with --use-k8s, "
            "an AgentSandboxBackend that provisions a sandbox pod)."
        ),
    ),
    JobStep(
        "sdk-call",
        "Run agent loop",
        (
            "Compile Configuration → ClaudeAgentOptions, build the in-process MCP "
            "server from the tool registry, drive claude_agent_sdk.query() until "
            "the agent emits a final answer. Most of the wall-clock time lives here."
        ),
    ),
    JobStep(
        "write-output",
        "Persist trajectory",
        "Serialize the Trajectory to runs/{task_id}.{config_name}.json.",
    ),
)

_COMPARE_STEPS: tuple[JobStep, ...] = (
    JobStep("load-task", "Load task", _RUN_STEPS[0].description),
    JobStep(
        "resolve-configs",
        "Resolve configurations A and B",
        "Look up both configurations and apply the optional model override to each.",
    ),
    JobStep("build-harness", "Build harness", _RUN_STEPS[2].description),
    JobStep(
        "sdk-call-a",
        "Run leg A",
        "Run the same task with configuration A. Captures one trajectory.",
    ),
    JobStep(
        "sdk-call-b",
        "Run leg B",
        "Run the same task with configuration B. Captures the second trajectory.",
    ),
    JobStep(
        "structural-report",
        "Compute structural diff",
        (
            "Rules-based comparison (event counts, failure-mode tags, success-criteria "
            "pass/fail). Free; runs without an LLM."
        ),
    ),
    JobStep(
        "judge",
        "LLM verdict (optional)",
        (
            "If a judge_model was provided, hand both trajectories + the structural "
            "report to inspect-ai's get_model() and ask for a JudgeVerdict. Adds one "
            "paid model call."
        ),
    ),
    JobStep(
        "write-output",
        "Persist outputs",
        "Write per-leg trajectories, the markdown structural report, and the verdict JSON.",
    ),
)


def _eval_steps(num_tasks: int) -> list[JobStep]:
    """Eval steps depend on the pack size; built dynamically."""
    steps: list[JobStep] = [
        JobStep(
            "load-pack",
            "Load task pack",
            f"Read the pack JSON, validate as a TaskPack ({num_tasks} task(s)).",
        ),
        JobStep("resolve-config", "Resolve configuration", _RUN_STEPS[1].description),
        JobStep("build-harness", "Build harness", _RUN_STEPS[2].description),
    ]
    for i in range(num_tasks):
        steps.append(
            JobStep(
                f"sdk-call-{i}",
                f"Run task {i + 1} of {num_tasks}",
                "Run one task in the pack with the selected configuration.",
            )
        )
    steps.extend(
        [
            JobStep(
                "aggregate",
                "Aggregate pack summary",
                (
                    "Compute completion rate, failure-mode frequencies, success-criteria "
                    "pass rates, tool-call statistics, total duration, total cost."
                ),
            ),
            JobStep(
                "write-output",
                "Persist outputs",
                "Write per-task trajectories and the markdown pack summary.",
            ),
        ]
    )
    return steps


# Harness factory protocol - mirrors the one in app.py but kept here so
# tests can inject a fake without importing the full FastAPI app.


class HarnessFactory(Protocol):
    def __call__(self, *, use_k8s: bool, k8s_namespace: str) -> _HarnessCtx: ...


class _HarnessCtx(Protocol):
    def __enter__(self) -> Harness: ...

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...


# --- runners (called inside the worker thread) -----------------------------


def _resolve_config(name: str, model_override: str) -> Configuration:
    cfg = configuration_by_name(name)
    if not model_override:
        return cfg
    return cfg.model_copy(update={"model": model_override})


def _sdk_call_detail(trajectory: object) -> str:
    """Detail string for the ``sdk-call*`` step's done event.

    Includes event count, optional cost / turn count from the SDK, and
    — when present — sandbox-pod telemetry so the live page makes it
    obvious *which* SDK calls actually hit the K8s pod.
    """
    parts = [f"recorded {len(trajectory.events)} event(s)"]  # type: ignore[attr-defined]
    cost = getattr(trajectory, "total_cost_usd", None)
    if cost:
        parts.append(f"cost ${cost:.6f}")
    turns = getattr(trajectory, "num_turns", None)
    if turns:
        parts.append(f"{turns} turn(s)")
    tel = getattr(trajectory, "sandbox_telemetry", None)
    if tel is not None:
        parts.append(
            f"sandbox: {tel.call_count} run_python call(s) in {tel.total_call_seconds:.2f}s"
        )
    return " • ".join(parts)


def _config_detail(cfg: Configuration) -> str:
    """One-line summary for the resolve step's detail field."""
    tool_summary = ", ".join(cfg.allowed_tools) if cfg.allowed_tools else "(none — orchestrator)"
    agent_summary = (
        f"{len(cfg.agents)} worker(s)" if cfg.agents else "no workers (single-agent ReAct)"
    )
    model = cfg.model or "(SDK default)"
    return f"tools: {tool_summary} • model: {model} • {agent_summary}"


def _build_harness_detail(use_k8s: bool, *configs: Configuration) -> str:
    """Detail string for the build-harness step.

    Surfaces whether the chosen execution backend will *actually* be
    exercised. ``AgentSandboxBackend`` is lazy: it provisions a pod the
    first time ``run_python`` runs. A configuration whose agents don't
    expose ``run_python`` makes ``--use-k8s`` a silent no-op, which is
    surprising. The job page now says so explicitly.
    """
    if not use_k8s:
        return "backend: LocalSubprocessBackend"
    if any(c.uses_run_python for c in configs):
        return (
            "backend: AgentSandboxBackend (k8s) — pod will be provisioned on the "
            "first run_python call"
        )
    names = ", ".join(c.name for c in configs)
    return (
        f"backend: AgentSandboxBackend (k8s) — but no agent in {names} exposes "
        "run_python, so no sandbox pod will be provisioned (the flag is a no-op "
        "for this configuration; pick single-agent-with-sandbox to actually exercise it)"
    )


@contextmanager
def _runs_output(runs_dir: Path) -> Iterator[Path]:
    runs_dir.mkdir(parents=True, exist_ok=True)
    yield runs_dir


def _do_run(
    job: Job,
    *,
    repo_root: Path,
    runs_dir: Path,
    factory: HarnessFactory,
) -> None:
    p = job.params

    job.emit("load-task", "running")
    task_path = (repo_root / p["task"]).resolve()
    task = Task.from_path(task_path)
    job.emit("load-task", "done", f"task_id={task.task_id} • prompt: {task.user_prompt[:80]}")

    job.emit("resolve-config", "running")
    cfg = _resolve_config(p["config"], p.get("model", ""))
    job.emit("resolve-config", "done", _config_detail(cfg))

    job.emit("build-harness", "running")
    job.emit(
        "build-harness",
        "done",
        _build_harness_detail(bool(p.get("use_k8s")), cfg),
    )

    with factory(
        use_k8s=bool(p.get("use_k8s")), k8s_namespace=p.get("k8s_namespace", "default")
    ) as harness:
        job.emit("sdk-call", "running", "calling claude_agent_sdk.query() — this is the slow phase")
        trajectory = harness.run(task, cfg)
        job.emit("sdk-call", "done", _sdk_call_detail(trajectory))

    job.emit("write-output", "running")
    with _runs_output(runs_dir) as out:
        out_path = out / f"{trajectory.task_id}.{cfg.name}.json"
        out_path.write_text(trajectory.model_dump_json(indent=2), encoding="utf-8")
    job.emit("write-output", "done", f"wrote {out_path.name}")
    job.redirect_url = f"/trajectories/{out_path.name}"


def _do_compare(
    job: Job,
    *,
    repo_root: Path,
    runs_dir: Path,
    factory: HarnessFactory,
) -> None:
    from harness_weaver.judge import StructuralReport, render_markdown

    p = job.params

    job.emit("load-task", "running")
    task_path = (repo_root / p["task"]).resolve()
    task = Task.from_path(task_path)
    job.emit("load-task", "done", f"task_id={task.task_id}")

    job.emit("resolve-configs", "running")
    cfg_a = _resolve_config(p["config_a"], p.get("model", ""))
    cfg_b = _resolve_config(p["config_b"], p.get("model", ""))
    job.emit("resolve-configs", "done", f"A: {cfg_a.name} • B: {cfg_b.name}")

    job.emit("build-harness", "running")
    detail = _build_harness_detail(bool(p.get("use_k8s")), cfg_a, cfg_b)
    job.emit("build-harness", "done", f"{detail} • single Harness, reused across both legs")

    trajs: list[Trajectory] = []
    with factory(
        use_k8s=bool(p.get("use_k8s")), k8s_namespace=p.get("k8s_namespace", "default")
    ) as harness:
        for index, (step_id, cfg) in enumerate((("sdk-call-a", cfg_a), ("sdk-call-b", cfg_b))):
            job.emit(step_id, "running", f"running configuration {cfg.name}")
            trajectory = harness.run(task, cfg)
            with _runs_output(runs_dir) as out:
                (out / f"{trajectory.task_id}.{cfg.name}.{index}.json").write_text(
                    trajectory.model_dump_json(indent=2), encoding="utf-8"
                )
            trajs.append(trajectory)
            job.emit(step_id, "done", _sdk_call_detail(trajectory))

    job.emit("structural-report", "running")
    report = StructuralReport.of(trajs[0], trajs[1], task=task)
    with _runs_output(runs_dir) as out:
        report_path = out / f"{task.task_id}.compare.md"
        report_path.write_text(render_markdown(report), encoding="utf-8")
    job.emit("structural-report", "done", "wrote markdown structural diff")

    judge_model = p.get("judge_model", "")
    if judge_model:
        from harness_weaver.judge.llm import InspectAILlmJudge

        job.emit("judge", "running", f"calling inspect-ai with {judge_model}")
        judge = InspectAILlmJudge(model=judge_model)
        verdict = asyncio.run(
            judge.verdict(task=task, trajectory_a=trajs[0], trajectory_b=trajs[1])
        )
        with _runs_output(runs_dir) as out:
            (out / f"{task.task_id}.compare.verdict.json").write_text(
                verdict.model_dump_json(indent=2), encoding="utf-8"
            )
        job.emit(
            "judge",
            "done",
            f"winner: {verdict.winner} • confidence: {verdict.confidence:.2f}",
        )
    else:
        job.emit("judge", "done", "skipped — no judge_model provided")

    job.emit(
        "write-output",
        "done",
        f"wrote {report_path.name} and 2 trajectory file(s)",
    )
    job.redirect_url = f"/reports/{report_path.name}"


def _do_eval(
    job: Job,
    *,
    repo_root: Path,
    runs_dir: Path,
    factory: HarnessFactory,
) -> None:
    from harness_weaver.judge import PackSummary, render_pack_markdown

    p = job.params

    job.emit("load-pack", "running")
    pack_path = (repo_root / p["pack"]).resolve()
    pack = TaskPack.from_path(pack_path)
    job.emit("load-pack", "done", f"pack: {pack.name} • {len(pack.tasks)} task(s)")

    job.emit("resolve-config", "running")
    cfg = _resolve_config(p["config"], p.get("model", ""))
    job.emit("resolve-config", "done", _config_detail(cfg))

    job.emit("build-harness", "running")
    detail = _build_harness_detail(bool(p.get("use_k8s")), cfg)
    job.emit("build-harness", "done", f"{detail} • single Harness, reused across all pack tasks")

    trajs: list[Trajectory] = []
    with factory(
        use_k8s=bool(p.get("use_k8s")), k8s_namespace=p.get("k8s_namespace", "default")
    ) as harness:
        for i, task in enumerate(pack.tasks):
            step_id = f"sdk-call-{i}"
            job.emit(step_id, "running", f"running task {task.task_id}")
            trajectory = harness.run(task, cfg)
            with _runs_output(runs_dir) as out:
                (out / f"{trajectory.task_id}.{cfg.name}.json").write_text(
                    trajectory.model_dump_json(indent=2), encoding="utf-8"
                )
            trajs.append(trajectory)
            job.emit(step_id, "done", _sdk_call_detail(trajectory))

    job.emit("aggregate", "running")
    summary = PackSummary.of(trajs, pack=pack, configuration_name=cfg.name)
    job.emit(
        "aggregate",
        "done",
        f"completed {summary.completed_count}/{summary.task_count}",
    )

    job.emit("write-output", "running")
    with _runs_output(runs_dir) as out:
        summary_path = out / f"{pack.name}.{cfg.name}.eval.md"
        summary_path.write_text(render_pack_markdown(summary), encoding="utf-8")
    job.emit("write-output", "done", f"wrote {summary_path.name}")
    job.redirect_url = f"/reports/{summary_path.name}"


# --- registry --------------------------------------------------------------


@dataclass
class JobRegistry:
    """In-memory job store + executor.

    Single-worker pool: jobs serialize. Concurrent submissions queue up
    on the executor side rather than fighting for the API key budget.
    """

    repo_root: Path
    runs_dir: Path
    factory: HarnessFactory
    _executor: ThreadPoolExecutor = field(
        default_factory=lambda: ThreadPoolExecutor(max_workers=1, thread_name_prefix="hw-job")
    )
    _jobs: dict[str, Job] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def submit_run(self, params: dict[str, Any]) -> Job:
        return self._submit(
            "run",
            f"Run task {params.get('task', '?')} with {params.get('config', '?')}",
            params,
            list(_RUN_STEPS),
            _do_run,
        )

    def submit_compare(self, params: dict[str, Any]) -> Job:
        return self._submit(
            "compare",
            f"Compare {params.get('config_a', '?')} vs {params.get('config_b', '?')} on {params.get('task', '?')}",
            params,
            list(_COMPARE_STEPS),
            _do_compare,
        )

    def submit_eval(self, params: dict[str, Any]) -> Job:
        # We need to peek at the pack to know how many sub-runs to plan.
        pack_path = (self.repo_root / params["pack"]).resolve()
        try:
            pack = TaskPack.from_path(pack_path)
            num = len(pack.tasks)
        except (OSError, ValueError):
            num = 0
        return self._submit(
            "eval",
            f"Eval pack {params.get('pack', '?')} with {params.get('config', '?')}",
            params,
            _eval_steps(num),
            _do_eval,
        )

    def _submit(
        self,
        job_type: JobType,
        title: str,
        params: dict[str, Any],
        steps: list[JobStep],
        runner: Callable[..., None],
    ) -> Job:
        job = Job(
            id=secrets.token_urlsafe(8),
            job_type=job_type,
            title=title,
            params=dict(params),
            steps=steps,
        )
        with self._lock:
            self._jobs[job.id] = job
        self._executor.submit(self._run_job, job, runner)
        return job

    def _run_job(self, job: Job, runner: Callable[..., None]) -> None:
        with job._lock:
            job.status = "running"
            job.started_at = datetime.now(UTC)
        try:
            runner(
                job,
                repo_root=self.repo_root,
                runs_dir=self.runs_dir,
                factory=self.factory,
            )
        except Exception as exc:
            # Catch ``Exception``, not ``BaseException``, so
            # KeyboardInterrupt / SystemExit propagate to the executor's
            # thread shutdown logic instead of being mis-recorded as a
            # "failed" job. PR #18 review.
            tb = traceback.format_exc()
            with job._lock:
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished_at = datetime.now(UTC)
            # Find the running step (if any) and mark it errored.
            # ``str(exc).splitlines()`` can be empty for bare
            # ``raise Exception()`` calls; fall back to the type name.
            # PR #18 review.
            exc_lines = str(exc).splitlines()
            first_line = exc_lines[0] if exc_lines else type(exc).__name__
            for step in job.steps:
                if step.status == "running":
                    job.emit(step.id, "error", first_line)
                    break
            # Always emit a job-level error event for the SSE stream.
            # Route through ``emit`` so the append happens under the
            # lock — direct ``job.events.append`` raced with a concurrent
            # ``snapshot()`` (PR #18 review).
            tb_lines = tb.splitlines()
            job.emit("__job__", "error", tb_lines[-1] if tb_lines else first_line)
        else:
            with job._lock:
                job.status = "done"
                job.finished_at = datetime.now(UTC)
            job.emit("__job__", "done", "")

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


# --- helpers exposed for testing ------------------------------------------


def configuration_summary(cfg_name: str) -> dict[str, Any]:
    """Used by the form pages' sidebar (JS-driven explainer panel).

    Returns a JSON-friendly dict the front-end can inject into the
    sidebar without a round-trip — the full shape of every built-in
    configuration is small enough to embed in the form page directly.
    """
    cfg = configuration_by_name(cfg_name)
    return {
        "name": cfg.name,
        "description": cfg.description,
        "model": cfg.model or "(SDK default)",
        "system_prompt": cfg.system_prompt,
        "allowed_tools": list(cfg.allowed_tools),
        "agents": [
            {
                "id": a.role_name,
                "system_prompt": a.system_prompt,
                "allowed_tools": list(a.allowed_tools),
            }
            for a in cfg.agents
        ],
        "is_multi_agent": cfg.is_multi_agent,
    }


__all__ = [
    "Job",
    "JobEvent",
    "JobRegistry",
    "JobStep",
    "configuration_summary",
]


# json is reserved for future use in snapshot helpers; keep the import
# discoverable for mypy.
_ = json

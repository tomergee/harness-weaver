# ADR-0003: Sandbox lifecycle — warm sandbox per Harness instance

## Status

Accepted — 2026-05-07

## Context

The harness exposes a ``run_python`` tool whose dangerous part —
executing arbitrary Python — runs through an
:class:`ExecutionBackend`. The dev backend
(:class:`LocalSubprocessBackend`) spawns a fresh subprocess per call;
that's cheap and the right shape for non-isolated dev work.

The production backend (:class:`AgentSandboxBackend`) provisions a
sandbox pod via ``kubernetes-sigs/agent-sandbox``. Pod startup costs
are non-trivial:

* image pull (50-300 MB; cached after first run)
* pod scheduling (5-15s on Kind, longer on real clusters under load)
* container start + readiness probe (5-30s)

A "fresh sandbox per call" architecture would charge that cost to
every single ``run_python`` tool invocation. Our analytical task pack
has runs that issue 5-20 ``run_python`` calls; at 30s/sandbox that's
2.5-10 minutes of pure scheduling latency per task — orders of
magnitude more than the actual computation.

We have to pick a lifecycle. The candidates:

1. **Per-call sandbox.** Every ``run_python`` provisions a fresh pod,
   runs one snippet, terminates. Maximum isolation between calls.
2. **Per-run sandbox.** One sandbox per ``Harness.run()``. Reused
   across the run's tool calls; terminated when the run finishes.
3. **Per-Harness sandbox.** One sandbox per :class:`Harness` instance.
   Reused across every run the harness executes; terminated on
   ``close()``.
4. **Persistent sandbox.** Cluster-level pool of long-lived sandboxes;
   the harness checks one out per run and returns it.

## Decision

**Adopt option 3: one sandbox per Harness instance, reused across
calls and across runs, terminated on ``close()``.**

Concretely:

* :class:`AgentSandboxBackend` lazily creates the sandbox on the
  first call to :meth:`run`. Subsequent calls reuse the same pod.
* The backend implements ``close()`` and the context-manager
  protocol; ``close()`` terminates the pod (configurable via
  ``cleanup_on_close``).
* Writable state (``/tmp``, the working directory, environment
  variables set by previous snippets) is **not** reset between
  calls. The agent's snippets share a working filesystem within a
  Harness lifetime.
* Snippets are file-staged to ``/tmp/snippet.py`` via the SDK's
  ``Filesystem`` API rather than passed via shell ``-c`` to avoid
  escaping pitfalls; the file is overwritten on each call.

## Consequences

**Easier:**

* **Latency amortizes.** A 30-second pod startup is paid once per
  Harness, not once per ``run_python`` call. For a five-call run,
  this is the difference between "few seconds total" and
  "~150 seconds of scheduling latency."
* **Trivial implementation.** ``_ensure_sandbox`` is a one-line
  cache; no pool, no checkout protocol, no liveness probing.
* **Predictable resource use.** One pod per harness CLI invocation;
  scales with the number of concurrent harnesses, not the number of
  tool calls.
* **Realistic agent semantics.** A real agent talking to a real
  workstation expects shared state: files written by one cell are
  there for the next. Per-call isolation breaks that intuition.

**Harder:**

* **No isolation between snippets within a run.** A snippet that
  writes ``/tmp/.bashrc`` or sets a destructive shell alias can
  affect later snippets in the same Harness. The mitigation is the
  agent contract: snippets are advisory and short-lived; if the
  trajectory shows the agent actively poisoning its own environment,
  that's a failure mode the judge should flag (and ADR-0006, when it
  lands, may codify per-snippet ``cd``-into-fresh-tmp behavior).
* **Pod cost lingers.** A long-running Harness (interactive use,
  REPL embedding) holds a sandbox for the whole session. The user
  has to call ``close()`` (or use ``with`` blocks) to release it.
  ``cleanup_on_close=False`` is provided for inspection scenarios.
* **State leaks across runs in a long-lived Harness.** If a single
  Harness instance executes ``run`` ten times, all ten share the
  sandbox. Tasks aren't independent at the filesystem level. For
  the CLI's one-Harness-per-invocation flow this doesn't matter; it
  matters for any future "long-lived service" mode, which is
  out-of-scope for v1.

## Alternatives rejected and why

* **Per-call (option 1)** — outright impractical at our typical
  call counts. Strict isolation isn't worth a 50× latency tax.
* **Per-run (option 2)** — would let multi-task evaluators reuse
  pods across runs but not within a single Harness's worth of runs.
  We'd save startup latency the same way option 3 does, but we'd
  lose the "one Harness, one process, one sandbox" mental model
  that makes CLI invocations easy to reason about. Re-creating the
  sandbox between runs of a 20-task pack would add ~10 minutes of
  scheduling for no isolation benefit (each pack run is already
  contained to one Harness).
* **Persistent pool (option 4)** — the right architecture for a
  multi-tenant production service, the wrong architecture for a
  portfolio-scale experimentation harness. Adds checkout protocol,
  warmth tracking, leak detection. Revisit if the harness is ever
  embedded in a long-lived service.

## Notes

* The ``cleanup_on_close=False`` escape hatch keeps the pod alive
  after ``close()`` returns; useful for "let me kubectl exec in and
  poke around" debugging. Default is ``True`` because we don't want
  orphaned pods.
* Stdin support is not yet wired in :class:`AgentSandboxBackend`
  (see ``run`` raising ``NotImplementedError`` when set). That's a
  separate, smaller decision: stage stdin to a file, prepend
  ``cat /tmp/.stdin | `` to the command, or wait for a real use
  case to clarify the contract.

## References

* :mod:`harness_weaver.execution.k8s` — implementation.
* :class:`harness_weaver.execution.LocalSubprocessBackend` — the
  per-call sibling backend; lifecycle is trivially "no state."
* `kubernetes-sigs/agent-sandbox <https://github.com/kubernetes-sigs/agent-sandbox>`_
  — upstream controller and CRDs.
* ADR-0001 — establishes the ADR format.
* ADR-0004 — MCP transport (in-process); orthogonal but adjacent.

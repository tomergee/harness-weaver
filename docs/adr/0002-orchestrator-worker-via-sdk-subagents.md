# ADR-0002: Orchestrator-worker topology via Claude Agent SDK subagents

## Status

Accepted — 2026-05-07

## Context

Some configurations in this harness are multi-agent: an orchestrator decomposes
a user prompt and delegates to one or more specialist workers (e.g. a
`Discovery` agent that explores the catalog and an `Explainer` agent that
turns retrieved candidates into a justified recommendation). The
`multi-agent-discovery-explainer` configuration is the first such example and
will not be the last.

To build that, we have to choose how the orchestrator launches and talks to
its workers. The candidate approaches:

1. **Custom orchestration in our code.** We hold worker session handles
   ourselves, route messages between them, and manage their lifecycle. Maximum
   flexibility, maximum surface area to maintain.
2. **Hierarchical subagents via the SDK's Task tool.** The orchestrator is a
   normal `query()` session; workers are subagents declared in
   `ClaudeAgentOptions` and invoked when the model calls the SDK-provided
   `Task` tool. The SDK owns subagent lifecycle, context isolation, and the
   hook surface.
3. **Peer-to-peer (A2A-style) messaging.** Workers can address each other
   directly, not just through the orchestrator. Symmetric topology.
4. **External graph framework (LangGraph or similar).** We declare nodes and
   edges; the framework runs the graph.

For the questions this harness is built to answer — *when does multi-agent
beat single-agent, and on what kinds of tasks?* — the topology variations we
care about are small in number and hierarchical in shape: one orchestrator,
one or two workers, possibly a serial pipeline (Discovery → Explainer). We do
not currently need peer messaging, dynamic graph rewriting, or worker pools.

We also care intensely about **trajectory legibility**. The judge runs over
trajectories, and the project's value proposition is that those trajectories
are auditable. Anything that puts orchestration logic outside the SDK's hook
surface — custom IPC, an external scheduler, a graph framework's internal
state — degrades that.

## Decision

We adopt **option 2: hierarchical subagents via the SDK's Task tool**, and
explicitly rule out (3) and (4) for the v1 scope.

Concretely:

- Workers are declared as `AgentDefinition` entries in the orchestrator's
  `ClaudeAgentOptions.agents` mapping (one per worker role).
- Each worker has its own system prompt, allowed tools, and (where it matters)
  scoped tool surface — i.e. an `Explainer` worker may not be allowed to call
  `run_python` even when the orchestrator can.
- The orchestrator decides when to invoke workers; we do not write our own
  routing or planning code on top of the SDK.
- All multi-agent runs go through a single `Harness.run()` entry point. The
  Harness is the only thing that calls the SDK; it does not know or care
  whether the underlying configuration is single- or multi-agent.
- Trajectory recording is unchanged: the same hook handlers fire for
  orchestrator and worker tool calls. The trajectory schema gains an
  `agent_id` field on each event so post-hoc analysis can attribute work
  correctly. (Schema details land with the recorder implementation; see the
  Tier 1 plan in HANDOFF.md.)

## Consequences

**Easier:**

- **Less code we own.** Subagent lifecycle, context isolation, and tool-call
  routing are the SDK's problem, not ours. The Harness shrinks to
  configuration compilation + `query()` invocation + recording.
- **Uniform trajectory capture.** Hook events fire the same way for
  orchestrator and worker calls. The recorder doesn't need a separate
  code path for the multi-agent case; it just tags events with `agent_id`.
- **Configurations stay declarative.** A multi-agent configuration is a
  `ClaudeAgentOptions` with extra `agents` entries plus a tweaked system
  prompt. No imperative orchestration code per configuration.
- **Debuggability.** A multi-agent run is a single SDK session tree, not a
  distributed system. Replaying a trajectory replays the whole thing, and
  the SDK's trace conventions apply uniformly.

**Harder:**

- **Hierarchical only.** Workers cannot directly address each other; all
  cross-worker coordination flows through the orchestrator. For the
  topologies in scope this is the right shape, but a future "research crew"
  or "debate" configuration would need to revisit this (new ADR).
- **Coupled to SDK evolution.** If the SDK changes `Task`-tool semantics or
  the `agents` config shape, we follow. Mitigation: pin a tested SDK range
  in `pyproject.toml`, treat SDK upgrades as their own PR.
- **Worker definitions live at compile time.** Workers are declared per
  configuration in `configurations.py`; we don't currently support an
  orchestrator inventing a new worker on the fly. This is a deliberate
  simplification — it keeps configurations comparable. If a future
  experiment needs runtime worker creation, we'd add it explicitly.
- **No native parallel fan-out aggregation.** The SDK's Task tool gives us
  serial delegation cleanly; parallel fan-out with structured aggregation
  (e.g. "ask three workers, vote") would need a thin convention on top
  (orchestrator launches N workers, then a final synthesis turn). Not
  needed for v1; flagged for a future ADR if we add a "voting" topology.

## Alternatives rejected and why

- **Custom orchestration (option 1).** Reinvents what the SDK already does
  well. The flexibility we'd buy isn't flexibility we need for the
  experiments in scope, and the maintenance cost is real.
- **A2A peer messaging (option 3).** No configuration in the v1 scope
  requires it. The added topology surface (cycles, deadlocks, message
  ordering) buys us nothing for hierarchical decompositions and would
  obscure the comparisons the harness exists to make.
- **External graph framework (option 4).** LangGraph et al. are a
  reasonable choice for projects whose differentiator *is* graph topology.
  Ours isn't. Adding a graph layer would put framework state outside the
  SDK's hook surface, hurting trajectory legibility, and would couple the
  project to a second async runtime model.

## Notes

- This ADR is about *how the orchestrator launches workers*, not about the
  sandbox lifecycle or tool transport. Sandbox lifecycle is ADR-0003;
  MCP-as-tool-transport is documented in the README's design-decisions
  section and may get its own ADR if it becomes contentious.
- "Subagents" here refers to the SDK's `agents` mechanism, not arbitrary
  threads of Claude. We don't spawn a separate `query()` session per worker.

## References

- Claude Agent SDK — `ClaudeAgentOptions.agents` and the `Task` tool.
- Anthropic, *Building effective agents*, 2024 — argues for the simplest
  topology that fits the task.
- ADR-0001 — establishes the ADR format used here.

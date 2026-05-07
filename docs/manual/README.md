# harness-weaver Manual

Working manual for `harness-weaver`. Read these in order if you're new;
jump around if you already know the basics.

## Contents

1. **[Getting started](getting-started.md)** — install, run your first task,
   verify the gate.
2. **[Concepts](concepts.md)** — Task, Configuration, Trajectory, Tool,
   ExecutionBackend, AgentRunner, Harness. The vocabulary the rest of the
   manual uses.
3. **[CLI reference](cli.md)** — `harness-weaver run / compare / eval /
   list-configs`. Flags, output paths, exit behavior.
4. **[Extending the harness](extending.md)** — write a new Task, define a
   Configuration, add a Tool, swap the ExecutionBackend.
5. **[Judging trajectories](judge.md)** — structural report, failure-mode
   classifier, LLM-as-judge with `inspect-ai`.
6. **[Troubleshooting](troubleshooting.md)** — known gotchas, especially
   around live SDK runs.

## Two-minute orientation

`harness-weaver` runs an LLM agent against a small movie catalog using
hierarchical subagents and an in-process MCP server. It records every
step of every run as a `Trajectory` so you can compare what the agent
did across configurations.

The unit of variation is the **Configuration** (system prompt + tool
surface + worker definitions), held against a constant model. Two runs
of the same Task on different Configurations differ in exactly the
thing you wanted to study.

For the design rationale, see the project [README](../../README.md). For
individual decisions, see the ADRs in [`docs/adr/`](../adr/).

## At a glance

```text
       Task                  Configuration              Trajectory
   (what to ask)         (how the agent runs)      (what it actually did)

  user_prompt   ─┐         system_prompt   ─┐         events:
  user_id        │         allowed_tools    │           UserMessage
  expected       │  ──>    agents (workers) │  ──>      AssistantTurn
  outcome        │         model            │           ToolUse / ToolResult
  success_       │                          │           FinalAnswer
   criteria      │                          │
```

`Harness.run(task, configuration)` is the one entry point that turns the
left two into the right one.

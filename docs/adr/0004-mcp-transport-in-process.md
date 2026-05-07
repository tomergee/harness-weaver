# ADR-0004: MCP transport — in-process, not stdio subprocess

## Status

Accepted — 2026-05-07

## Context

The harness exposes its tool surface (catalog tools, sandboxed
``run_python``) to the Claude Agent SDK via MCP. The SDK supports four
MCP server transports:

1. **In-process** (``create_sdk_mcp_server``) — the server runs in the
   same Python process as the harness; tool calls are direct async
   function invocations.
2. **Stdio subprocess** (``McpStdioServerConfig``) — the SDK launches
   the server as a child process and communicates via JSON-RPC over
   stdin/stdout.
3. **SSE** (``McpSSEServerConfig``) — server is reachable over HTTP
   Server-Sent Events.
4. **HTTP** (``McpHttpServerConfig``) — server is reachable over plain
   HTTP.

Our tools are not abstract: they hold references to a loaded
:class:`Catalog` (60 movies, 73 ratings) and an
:class:`ExecutionBackend` (the subprocess runner for ``run_python``).
Whichever transport we pick has to make those references reachable from
the side that actually executes the tools.

ADR-0002 already committed the harness to the SDK's hierarchical
subagent model; this ADR is about *transport*, not *topology*.

## Decision

**Use in-process MCP** via :func:`claude_agent_sdk.create_sdk_mcp_server`,
keyed under the server name ``"harness_weaver"`` in
:attr:`ClaudeAgentOptions.mcp_servers`. The implementation lives in
:mod:`harness_weaver.mcp_server`.

Each Tool in the registry is wrapped as an ``SdkMcpTool`` via the
``@tool`` decorator. The wrapper:

* delegates to ``Tool.call(arguments)`` for execution,
* packages the JSON return value as MCP ``content`` (single text block),
* converts ``ToolError`` to ``{"is_error": True, "content": [...]}``.

Stdio, SSE, and HTTP are explicitly rejected for v1.

## Consequences

**Easier:**

* **Direct access to harness state.** Tools share the harness's
  ``Catalog`` and ``ExecutionBackend`` instances. No registry rebuild in
  a child process; no IPC for sandbox execution; no env-var-shaped
  protocol for telling the child where the data lives.
* **One process to debug.** When something goes wrong, a single
  traceback covers harness, MCP layer, and tool execution. No tailing
  child-process stderr.
* **Trajectory recording stays simple.** SDK hooks fire in our process,
  the recorder lives in our process, all writes happen in-band. Stdio
  would have required either shipping events back across the pipe or
  duplicating recording logic in the child.
* **No subprocess management.** No spawn, no zombie children, no
  Windows-vs-POSIX divergence in process control.

**Harder:**

* **No process-level isolation between MCP and harness.** A tool that
  segfaults the interpreter takes the harness down with it. For our
  tool set (typed Python over an in-memory dict, plus a delegated
  subprocess for ``run_python``), this is acceptable — the actual
  sandboxing seam is :class:`ExecutionBackend`, one layer down.
* **Cannot be reused as an external MCP server.** A different MCP
  client (Claude Code, another agent runtime) cannot connect to our
  registry without us also publishing a stdio entry point. If that
  becomes a requirement we'd add a parallel
  :mod:`harness_weaver.mcp_server.stdio` module that re-exposes the
  same registry — the wrapper code is the same; only the bootstrap
  changes.
* **Coupled to the SDK's MCP-server abstraction.** If the SDK changes
  ``create_sdk_mcp_server``'s contract or removes it in favor of
  pure stdio, we follow.

## Alternatives rejected and why

* **Stdio subprocess (option 2).** Would require either re-loading the
  catalog in the child, or shipping a serialized catalog over the pipe
  on startup; either way, the ``run_python`` path either gets an
  isolated execution backend (good for safety, painful for shared
  trajectory recording) or the harness becomes a router between two
  subprocesses (one for MCP, one for ``run_python``). Cost without
  benefit for the use case in scope.
* **SSE / HTTP (options 3 and 4).** Network transports for MCP make
  sense when the server is a remote service. Ours is a Python module in
  the same repo; an HTTP layer would only add a port and a serializer.
* **Skip MCP, use the SDK's built-in tool registration (`tools=` with
  ``@tool``-decorated functions directly).** Possible, but it would
  collapse the seam ADR-0002 and the README design notes care about —
  *tool definition* and *tool transport* would no longer be separately
  swappable. Keeping the MCP boundary preserves that separation even
  though both sides are in-process today.

## Notes

* The MCP server name (``"harness_weaver"``) is a constant in
  :mod:`harness_weaver.mcp_server`; tests assert against it. Don't
  rename it without thinking through cassette compatibility.
* Tool naming: the SDK exposes MCP tools to the model under prefixes
  it manages internally, but ``ClaudeAgentOptions.allowed_tools`` takes
  bare names (matching the calculator example in
  ``create_sdk_mcp_server`` 's docstring). Our compiler in
  :mod:`harness_weaver.sdk_compile` passes bare names through.

## References

* :func:`claude_agent_sdk.create_sdk_mcp_server`
* ADR-0002 — orchestrator-worker via SDK subagents.
* :mod:`harness_weaver.mcp_server` — implementation.
* :mod:`harness_weaver.sdk_compile` — Configuration → ClaudeAgentOptions
  compiler that consumes the server.

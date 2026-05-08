# Contributing

Thanks for poking at this. Here's the shortest path from clone to PR.

## Setup

```bash
git clone https://github.com/tomergee/harness-weaver
cd harness-weaver
make install              # installs the package in editable mode + pre-commit hooks
```

`ANTHROPIC_API_KEY` is **only** needed for live SDK runs (`harness-weaver
run/compare/eval` against a real model) and for recording cassettes.
The full test suite runs without one.

## The gate

Before opening a PR:

```bash
make check
```

That runs `fmt-check + lint + typecheck + test` — the same set CI runs
on push. `make check` is intentionally non-mutating so a locally-passing
run cannot fail CI on the same files.

If the gate fails:

* **Formatting drift?** `make fmt` rewrites files in place to match.
  Then re-run `make check`.
* **Lint errors?** `ruff check src tests --fix` autofixes the easy
  ones; manual edits for the rest. (`make fmt` deliberately doesn't
  run `--fix` — it only handles formatting, so a fresh format pass
  doesn't accidentally reshape code beyond layout.)
* **Mypy or pytest failures?** Read the error and fix the underlying
  issue. No autofix here.

Coverage is gated at 70% in `pyproject.toml`; CI rejects regressions.

## Branching and commits

* Branch off `main`. Names are scoped:
  `claude/<short-topic>` for AI-assisted work, `<topic>` for manual.
* Conventional-commit prefixes are encouraged but not enforced:
  `feat:`, `fix:`, `docs:`, `chore:`, `test:`, `refactor:`.
* Keep commits focused. A bug fix and a refactor are two commits.

## PRs

* Every PR runs the gate via GitHub Actions.
* The first review pass usually comes from
  [`gemini-code-assist`](https://github.com/apps/gemini-code-assist),
  which leaves inline comments. Address them with code fixes plus
  inline replies; don't squash review history into one "addressed
  feedback" commit.
* When a change touches an architectural seam (execution backend, MCP
  transport, judge, etc.) consider an ADR under
  [`docs/adr/`](docs/adr/). The format is consistent across the
  existing five — copy whichever is closest in scope.

## Where things live

* `src/harness_weaver/` — production code.
  * `agent_runner.py` — `RealAgentRunner` (SDK) and `FakeAgentRunner`.
  * `harness.py` — orchestrates one run end-to-end.
  * `tools/`, `mcp_server.py` — tool surface and MCP transport.
  * `execution/` — `LocalSubprocessBackend` and `AgentSandboxBackend`.
  * `judge/` — structural diagnostics + LLM judge (inspect-ai).
* `tests/` — mirrors the source tree.
* `docs/manual/` — user-facing manual.
* `docs/adr/` — architecture decision records.
* `examples/` — task JSON, task packs, sample trajectories.
* `scripts/` — `install-agent-sandbox.sh`, `kind-up.sh`, `kind-down.sh`.

## Things to avoid

* Live API calls in tests. Either mock at the agent-runner seam
  (`FakeAgentRunner`) or replay via vcrpy cassettes. CI runs without an
  API key.
* Loosening `mypy`'s strict mode globally. Add a per-module override
  with a comment explaining why if you really need it.
* New top-level packages without an ADR. The existing module shape is
  load-bearing for the design notes in the README.

## Questions

Open an issue. Tag it `question`.

# Handoff: Copy these files into your repo

This folder contains the initial scaffolding for `harness-weaver`. Copy
everything here into your local clone of the repo, then run a few
verification commands.

## Layout produced

```
harness-weaver-scaffold/
├── .github/
│   └── workflows/
│       └── ci.yml
├── .gitignore
├── .pre-commit-config.yaml
├── LICENSE
├── Makefile
├── README.md
├── docs/
│   └── adr/
│       └── 0001-record-architecture-decisions.md
├── examples/
│   └── README.md
├── pyproject.toml
├── src/
│   └── harness_weaver/
│       ├── __init__.py
│       ├── cli.py
│       └── py.typed
└── tests/
    ├── __init__.py
    ├── conftest.py
    └── test_cli.py
```

## One-shot copy

Assuming your local clone is at `~/code/harness-weaver` (adjust as needed):

### macOS / Linux

```bash
SCAFFOLD="<path-to-this-folder>"      # e.g. "/path/to/harness-weaver-scaffold"
REPO="$HOME/code/harness-weaver"

cp -R "$SCAFFOLD"/. "$REPO"/

cd "$REPO"
git add .
git status                             # sanity check before committing
```

### Windows (PowerShell)

```powershell
$Scaffold = "<path-to-this-folder>"   # e.g. "D:\...\harness-weaver-scaffold"
$Repo     = "$HOME\code\harness-weaver"

Copy-Item -Path "$Scaffold\*" -Destination $Repo -Recurse -Force

Set-Location $Repo
git add .
git status
```

## Verification (run from inside the repo)

```bash
# 1. Create a venv (recommended)
python3.11 -m venv .venv
source .venv/bin/activate              # PowerShell: .venv\Scripts\Activate.ps1

# 2. Install the package and dev deps
make install

# 3. Run the gate
make check
```

Expected: ruff format check passes, ruff lint passes, mypy --strict passes,
pytest passes (3 tests, ≥70% coverage of the trivial `__init__` and
`cli` modules).

## What you should commit

A clean first commit message that signals intent:

```
chore: scaffold harness-weaver

- Python 3.11+ project layout (src/, tests/, docs/, examples/)
- pyproject.toml with hatchling, ruff, mypy --strict, pytest
- Pre-commit hooks (ruff format/lint, basic file hygiene)
- GitHub Actions CI on Python 3.11 and 3.12
- README with architecture, configurations, and design decisions
- ADR-0001 establishing the ADR format
- Typer-based CLI stubs for run/compare/eval
- Smoke tests for CLI wiring

Implementation of the harness, configurations, tools, and judge follows in
subsequent commits.
```

## Important: commit history hygiene

Make this the **only** "scaffold everything" commit. Subsequent commits
should be focused and named for the feature they introduce. Reviewers
(including future you) will appreciate the granularity. Examples of next
commits:

- `feat: add Tool and ToolSchema base types`
- `feat: add LocalSubprocessBackend`
- `feat: add SearchTitlesTool with MovieLens loader`
- `feat: add MCP server wrapping the Tool registry`
- `feat: wire claude-agent-sdk session in Harness`
- `test: add cassette-backed e2e for single-agent-basic`

Avoid mega-commits. Avoid `git push --force` on `main`.

## Known caveats to address before Tier 1 ships

These are flagged in the design notes but worth re-stating here:

1. **Verify `claude-agent-sdk` package name and minimum version** against
   PyPI before relying on it. The package was renamed from `claude-code-sdk`;
   the version specifier in `pyproject.toml` may need adjustment.

2. **Verify `k8s-agent-sandbox` package availability and version**. If the
   PyPI listing is sparse or outdated, you may need to adjust the version
   spec or install from source for the Tier 2/3 demo path.

3. **Verify `inspect-ai` install footprint**. It pulls in significant deps
   (HuggingFace, etc.). If install size becomes a problem, consider moving
   `inspect-ai` to an optional `[eval]` extra rather than core dependencies.

4. **Confirm SDK hook surface** before implementing the `TrajectoryRecorder`.
   The hook names sketched in the interface (`on_pre_tool_use`,
   `on_post_tool_use`, etc.) are conservative guesses; align with the
   actual SDK API.

## Next deliverables (in order)

After this scaffolding lands:

1. **ADR-0002** — orchestrator-worker via SDK subagents (the multi-agent
   topology decision). ~30 min.
2. **ADR-0003** — sandbox lifecycle: one per Harness, reset between tasks
   (vs per-task). ~20 min.
3. **Tool layer** — `Tool` protocol, `ToolSchema`, three pure-data tools
   (`search_titles`, `get_metadata`, `user_history`) backed by a small
   MovieLens or TMDB CSV. With unit tests.
4. **ExecutionBackend** — `LocalSubprocessBackend` first; `AgentSandboxBackend`
   in a follow-up commit.
5. **MCP server** — wraps the Tool registry, registered with the SDK as
   stdio-transport.
6. **Harness skeleton** — Configuration → ClaudeAgentOptions, a single
   `run()` path that produces a real Trajectory.

That's Tier 1. After that, multi-agent and the judge pipeline are Tier 2.

#!/usr/bin/env python3
"""Record an SDK message-stream cassette for one task + configuration.

Run once with ``ANTHROPIC_API_KEY`` set. The output goes to
``tests/cassettes/<task_id>.<config_name>.pkl`` and gets committed.
The CI replay test (:mod:`tests.test_e2e_cassette`) consumes it via
:func:`tests._cassette.replay_query`.

Usage:

    ANTHROPIC_API_KEY=sk-ant-... python scripts/record-cassette.py \\
        examples/tasks/discovery-mood-tense.json \\
        --config single-agent-basic \\
        --model claude-haiku-4-5-20251001

A cassette captures one specific (task, configuration, model) tuple.
Re-record after a meaningful SDK upgrade or a configuration change.
"""

from __future__ import annotations

import argparse
import asyncio  # noqa: F401  (kept for direct-driver use cases)
import sys
from pathlib import Path

# Make the `tests` package importable so we can pull the cassette helpers
# in from a script that lives outside the test suite.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from harness_weaver.agent_runner import RealAgentRunner  # noqa: E402
from harness_weaver.catalog import Catalog  # noqa: E402
from harness_weaver.configurations import configuration_by_name  # noqa: E402
from harness_weaver.harness import Harness  # noqa: E402
from harness_weaver.task import Task  # noqa: E402
from tests._cassette import record_query  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_path", type=Path, help="Path to a Task JSON file.")
    parser.add_argument("--config", default="single-agent-basic", help="Configuration name.")
    parser.add_argument(
        "--model",
        default="claude-haiku-4-5-20251001",
        help="Model id. Override from the configuration default.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "tests" / "cassettes",
        help="Directory for the cassette pickle.",
    )
    args = parser.parse_args()

    task = Task.from_path(args.task_path)
    cfg = configuration_by_name(args.config).model_copy(update={"model": args.model})

    cassette_path = args.output_dir / f"{task.task_id}.{cfg.name}.pkl"
    print(f"Recording cassette to {cassette_path}", file=sys.stderr)

    runner = RealAgentRunner(query_fn=record_query(cassette_path))
    harness = Harness(catalog=Catalog.load_default(), runner=runner)
    trajectory = harness.run(task, cfg)
    print(
        f"Recorded {len(trajectory.events)} trajectory events "
        f"(final_answer present: {trajectory.final_answer is not None}).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

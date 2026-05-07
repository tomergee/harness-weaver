"""Unit tests for the Task and TaskPack JSON loaders."""

import json
from pathlib import Path

import pytest

from harness_weaver.task import Task, TaskPack


class TestTaskLoading:
    def test_loads_minimal_task(self, tmp_path: Path) -> None:
        path = tmp_path / "t.json"
        path.write_text(json.dumps({"task_id": "t1", "user_prompt": "hi"}))
        task = Task.from_path(path)
        assert task.task_id == "t1"
        assert task.user_prompt == "hi"
        assert task.user_id is None
        assert task.tags == []

    def test_loads_full_task(self, tmp_path: Path) -> None:
        path = tmp_path / "t.json"
        path.write_text(
            json.dumps(
                {
                    "task_id": "t1",
                    "user_prompt": "hi",
                    "user_id": "u1",
                    "expected_outcome": "good answer",
                    "success_criteria": {"min_results": 1},
                    "tags": ["discovery"],
                }
            )
        )
        task = Task.from_path(path)
        assert task.user_id == "u1"
        assert task.success_criteria == {"min_results": 1}
        assert task.tags == ["discovery"]

    def test_extra_field_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "t.json"
        path.write_text(json.dumps({"task_id": "t1", "user_prompt": "hi", "bogus": 1}))
        with pytest.raises(ValueError, match="extra"):
            Task.from_path(path)

    def test_missing_required_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "t.json"
        path.write_text(json.dumps({"task_id": "t1"}))  # no user_prompt
        with pytest.raises(ValueError, match="user_prompt"):
            Task.from_path(path)

    def test_bundled_examples_load(self) -> None:
        # Sanity check: the JSON files we ship in examples/tasks/ must be valid.
        repo_root = Path(__file__).parent.parent
        for path in (repo_root / "examples/tasks").glob("*.json"):
            task = Task.from_path(path)
            assert task.task_id == path.stem


class TestTaskPack:
    def test_loads(self, tmp_path: Path) -> None:
        path = tmp_path / "p.json"
        path.write_text(
            json.dumps(
                {
                    "name": "smoke",
                    "description": "just one task",
                    "tasks": [{"task_id": "t1", "user_prompt": "hi"}],
                }
            )
        )
        pack = TaskPack.from_path(path)
        assert pack.name == "smoke"
        assert len(pack.tasks) == 1
        assert pack.tasks[0].task_id == "t1"

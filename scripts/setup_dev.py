#!/usr/bin/env python3
"""Create a local venv and install harness-weaver with dev dependencies.

Run from the repository root:

    python scripts/setup_dev.py

Requires Python 3.11+. Creates ``.venv`` if it does not exist, then runs
``pip install -U pip``, ``pip install -e ".[dev]"``, and ``pre-commit install``.
"""

from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def venv_dir(root: Path) -> Path:
    return root / ".venv"


def venv_python(venv_path: Path) -> Path:
    if sys.platform == "win32":
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"


def run(py: Path, cwd: Path, *args: str) -> None:
    cmd = [str(py), *args]
    print("+", " ".join(cmd))
    subprocess.check_call(cmd, cwd=cwd)


def main() -> int:
    root = repo_root()
    venv_path = venv_dir(root)

    if not venv_path.is_dir():
        print("Creating .venv …")
        venv.EnvBuilder(with_pip=True).create(venv_path)

    py = venv_python(venv_path)
    if not py.exists():
        print(f"error: expected venv interpreter at {py}", file=sys.stderr)
        return 1

    run(py, root, "-m", "pip", "install", "-U", "pip")
    run(py, root, "-m", "pip", "install", "-e", ".[dev]")

    try:
        run(py, root, "-m", "pre_commit", "install")
    except subprocess.CalledProcessError:
        print("warning: pre-commit install failed (optional)", file=sys.stderr)

    print()
    print("Setup finished. Activate the venv, then run the quality gate:")
    if sys.platform == "win32":
        print(f"  PowerShell: .\\{venv_path.name}\\Scripts\\Activate.ps1")
        print(f"  cmd.exe:    {venv_path / 'Scripts' / 'activate.bat'}")
        print(
            "  python -m ruff format src tests && python -m ruff check src tests "
            "&& python -m mypy src && python -m pytest"
        )
    else:
        print(f"  source {venv_path / 'bin' / 'activate'}")
        print("  make check")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"error: command failed with exit code {exc.returncode}", file=sys.stderr)
        raise SystemExit(exc.returncode) from exc

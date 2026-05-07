.PHONY: help install fmt lint typecheck test test-cov check clean

help:
	@echo "Targets:"
	@echo "  install     install package and dev deps; install pre-commit hooks"
	@echo "  fmt         format code with ruff"
	@echo "  lint        lint code with ruff"
	@echo "  typecheck   run mypy --strict"
	@echo "  test        run pytest"
	@echo "  test-cov    run pytest with coverage report"
	@echo "  check       fmt + lint + typecheck + test (the gate before opening a PR)"
	@echo "  clean       remove build artifacts and caches"

install:
	pip install -e ".[dev]"
	pre-commit install

fmt:
	ruff format src tests

lint:
	ruff check src tests

typecheck:
	mypy src

test:
	pytest

test-cov:
	pytest --cov-report=html
	@echo "Coverage report: htmlcov/index.html"

check: fmt lint typecheck test
	@echo "All checks passed."

clean:
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	rm -rf .mypy_cache .ruff_cache .pytest_cache htmlcov coverage.xml .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +

.PHONY: help install fmt fmt-check lint typecheck test test-cov check clean kind-up kind-down install-sandbox

help:
	@echo "Targets:"
	@echo "  install          install package and dev deps; install pre-commit hooks"
	@echo "  fmt              format code with ruff (mutating; run before committing)"
	@echo "  fmt-check        check formatting without mutating (matches CI)"
	@echo "  lint             lint code with ruff"
	@echo "  typecheck        run mypy --strict"
	@echo "  test             run pytest"
	@echo "  test-cov         run pytest with coverage report"
	@echo "  check            fmt-check + lint + typecheck + test (the gate before opening a PR; matches CI)"
	@echo "  clean            remove build artifacts and caches"
	@echo "  install-sandbox  install agent-sandbox controller + python template into the current kubectl context"
	@echo "  kind-up          bring up a fresh Kind cluster AND install the controller + template"
	@echo "  kind-down        tear the local Kind cluster down"

install:
	pip install -e ".[dev]"
	pre-commit install

fmt:
	ruff format src tests

fmt-check:
	ruff format --check src tests

lint:
	ruff check src tests

typecheck:
	mypy src

test:
	pytest

test-cov:
	pytest --cov-report=html
	@echo "Coverage report: htmlcov/index.html"

check: fmt-check lint typecheck test
	@echo "All checks passed."

clean:
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	rm -rf .mypy_cache .ruff_cache .pytest_cache htmlcov coverage.xml .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +

install-sandbox:
	./scripts/install-agent-sandbox.sh

kind-up:
	./scripts/kind-up.sh

kind-down:
	./scripts/kind-down.sh

"""harness-weaver — experimentation harness for agentic systems on recommendation-style tasks."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("harness-weaver")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]

"""Optional web UI for harness-weaver.

Install ``harness-weaver[web]`` and run ``harness-weaver serve`` to get
a flat HTML interface for kicking off runs and browsing trajectories.

The UI is intentionally minimal: server-rendered Jinja2 templates, one
small CSS file, no JS framework. It's a "click around and look at
things" surface for demos, not a production UX.

The ``app`` factory (:func:`create_app`) is the only public entry point.
The CLI ``serve`` command imports it lazily so the core install can stay
lean.
"""

from harness_weaver.web.app import create_app

__all__ = ["create_app"]

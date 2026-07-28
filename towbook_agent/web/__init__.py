"""FastAPI dashboard.

Templates live in ``web/templates`` and assets in ``web/static``. Chart.js and
HTMX are vendored as local static files -- there is no build step and no CDN
dependency, so the dashboard works on a shop machine with no internet.

The app object is created in ``web.app``; it is not imported here so that
``towbook_agent.web`` stays importable without FastAPI installed.
"""

from __future__ import annotations

__all__: list[str] = []

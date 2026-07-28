"""Deterministic core: paths, config, logging, sandboxed rule evaluation,
database models, session handling and the event bus.

Nothing in this package talks to an LLM and nothing here reaches the network
except the database driver. Submodules are imported explicitly by their users
rather than re-exported here, so that importing ``towbook_agent.core.paths``
does not drag SQLAlchemy or PyYAML into the process.
"""

from __future__ import annotations

__all__: list[str] = []

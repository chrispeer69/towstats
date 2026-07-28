"""The pipeline agents.

    acquisition -> ingestion -> classifier -> metrics -> analyst -> notifier

Hard constraint #3: exactly one of these uses an LLM. The Analyst writes prose
and proposes rule changes; acquisition, ingestion, classification and metrics
are fully deterministic, so the same input window always produces the same
numbers.

Submodules are not imported here. ``acquisition`` pulls in Playwright and
``analyst`` pulls in the Anthropic SDK, and neither should be a cost paid by a
process that only wants to read metrics.
"""

from __future__ import annotations

__all__: list[str] = []

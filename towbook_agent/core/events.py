"""Event bus between the pipeline and the notifier.

Hard constraint #5: pipeline failure must alert, and silence is never treated
as success. That makes this module the one place in the codebase where an
exception must never escape and never be swallowed quietly:

* every handler runs inside its own try/except, so one dead channel cannot stop
  the others from delivering;
* before any handler runs, the event is appended to ``state/events.jsonl``, so
  even a total notifier failure leaves a durable, timestamped trace;
* if *no* handler succeeded, the module logs at CRITICAL. A missing report has
  to be louder than a bad one.

The notifier is imported lazily inside the call rather than at module import,
which keeps ``core`` free of any dependency on ``agents`` and lets the
foundation be used before the notifier exists.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Final

from .paths import STATE_DIR

__all__ = [
    "emit_event",
    "emit_pipeline_failure",
    "emit_alert",
    "register_handler",
    "unregister_handler",
    "clear_handlers",
    "EVENT_TYPES",
    "PIPELINE_FAILURE",
    "ALERT",
]

logger = logging.getLogger(__name__)

PIPELINE_FAILURE: Final[str] = "pipeline_failure"
ALERT: Final[str] = "alert"

#: The complete event vocabulary. Anything else is a programming error.
EVENT_TYPES: Final[frozenset[str]] = frozenset({PIPELINE_FAILURE, ALERT})

EventHandler = Callable[[str, dict], None]

_handlers: list[EventHandler] = []
_lock = threading.RLock()

_EVENT_LOG = STATE_DIR / "events.jsonl"


# --------------------------------------------------------------------------
# Handler registry
# --------------------------------------------------------------------------


def register_handler(handler: EventHandler) -> None:
    """Add an extra event sink. The notifier is always called regardless.

    Used by tests (to capture events) and by the dashboard (to surface recent
    events live).
    """
    with _lock:
        if handler not in _handlers:
            _handlers.append(handler)


def unregister_handler(handler: EventHandler) -> None:
    with _lock:
        if handler in _handlers:
            _handlers.remove(handler)


def clear_handlers() -> None:
    with _lock:
        _handlers.clear()


# --------------------------------------------------------------------------
# Durable trace
# --------------------------------------------------------------------------


def _append_to_event_log(event_type: str, payload: dict) -> None:
    """Append the event to state/events.jsonl. Best effort, never raises."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event_type": event_type,
        "payload": payload,
    }
    try:
        _EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str, ensure_ascii=False)
        with _EVENT_LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception as exc:  # pragma: no cover - disk level failure
        logger.warning("could not append to event log %s: %s", _EVENT_LOG, exc)


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


def _notifier_sink(event_type: str, payload: dict) -> None:
    """Route the event to agents.notifier.dispatch_event.

    Imported here rather than at module scope so that core never depends on
    agents at import time, and so a partially built system still runs.
    """
    from ..agents import notifier  # local import, deliberate

    notifier.dispatch_event(event_type, payload)


def emit_event(event_type: str, payload: dict) -> None:
    """Emit an event to the notifier and to every registered handler.

    event_type must be one of EVENT_TYPES ("pipeline_failure" or "alert").
    Never raises: a failure to notify is logged at CRITICAL, because the caller
    is usually already handling a failure of its own and must not be derailed.
    """
    if event_type not in EVENT_TYPES:
        # A bad event type is a bug in our own code, and it is better to shout
        # about it than to silently drop a failure alert.
        logger.error(
            "unknown event_type %r (expected one of %s); emitting anyway",
            event_type,
            ", ".join(sorted(EVENT_TYPES)),
        )

    payload = dict(payload or {})
    payload.setdefault("event_type", event_type)
    payload.setdefault("emitted_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))

    _append_to_event_log(event_type, payload)

    with _lock:
        extra_handlers = list(_handlers)

    delivered = 0
    failures: list[str] = []

    for handler in [_notifier_sink, *extra_handlers]:
        name = getattr(handler, "__name__", repr(handler))
        try:
            handler(event_type, payload)
            delivered += 1
        except Exception as exc:
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
            logger.exception("event handler %s failed for %s", name, event_type)

    if delivered == 0:
        logger.critical(
            "EVENT NOT DELIVERED: %s could not be sent to any handler. "
            "Payload was written to %s. Failures: %s",
            event_type,
            _EVENT_LOG,
            "; ".join(failures) or "no handlers registered",
        )
    else:
        logger.info("event %s delivered to %d handler(s)", event_type, delivered)


def emit_pipeline_failure(stage: str, error: BaseException | str, **context: Any) -> None:
    """Convenience wrapper for the most important event in the system.

    stage   -- which part broke ("acquisition", "ingestion", "metrics", ...)
    error   -- the exception, or a message
    context -- anything useful: run_id, window_start, account_id, xlsx path
    """
    payload: dict[str, Any] = {
        "stage": stage,
        "error": f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error),
        "severity": "high",
    }
    payload.update(context)
    emit_event(PIPELINE_FAILURE, payload)


def emit_alert(alert_id: str, severity: str, entity: str = "", **context: Any) -> None:
    """Convenience wrapper for a rule-driven alert from config/rules.yaml."""
    payload: dict[str, Any] = {
        "alert_id": alert_id,
        "severity": severity,
        "entity": entity,
    }
    payload.update(context)
    emit_event(ALERT, payload)

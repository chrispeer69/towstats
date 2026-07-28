"""Logging configuration plus a filter that keeps secrets out of the logs.

Hard constraint #1: credentials come from the environment only, and are never
logged. Discipline alone is not enough -- a Playwright timeout can dump a form
payload, an HTTP client can echo a URL with a token in it. So every handler
installed here carries a filter that scans each record for the *values* of the
sensitive environment variables and replaces them with ``***REDACTED***``.

The filter is attached to the handlers rather than to the root logger, because
a Filter on a logger is not consulted for records that propagate up from child
loggers, while a Filter on a handler sees everything that handler emits.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Final, Iterable

from .paths import LOG_DIR

__all__ = [
    "setup_logging",
    "get_logger",
    "redact",
    "SecretRedactingFilter",
    "SENSITIVE_ENV_KEYS",
    "REDACTION",
]

#: Environment variables whose *values* must never appear in a log record.
SENSITIVE_ENV_KEYS: Final[tuple[str, ...]] = (
    "TOWBOOK_PASS",
    "ANTHROPIC_API_KEY",
    "TWILIO_AUTH_TOKEN",
    "SMTP_PASS",
    # Not named in the spec but equally dangerous if they are ever populated.
    "TWILIO_ACCOUNT_SID",
    "TOWBOOK_ACCOUNTS",
)

REDACTION: Final[str] = "***REDACTED***"

_DEFAULT_FORMAT: Final[str] = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
_DEFAULT_DATEFMT: Final[str] = "%Y-%m-%dT%H:%M:%S%z"

# A short value is not worth redacting -- a one or two character password would
# match everywhere and turn the log into confetti. Anything this short is not a
# real credential.
_MIN_SECRET_LENGTH: Final[int] = 4

_configured: bool = False
_exception_formatter = logging.Formatter()


def _current_secrets() -> list[str]:
    """Collect the current values of the sensitive environment variables.

    Re-read on every record rather than cached at import time, because .env
    loading and credential rotation can both happen after this module is
    imported.
    """
    secrets: list[str] = []
    for key in SENSITIVE_ENV_KEYS:
        value = os.environ.get(key)
        if value and len(value.strip()) >= _MIN_SECRET_LENGTH:
            secrets.append(value.strip())
    # Longest first, so that a secret which contains another secret as a
    # substring is masked completely rather than partially.
    secrets.sort(key=len, reverse=True)
    return secrets


def redact(text: str, secrets: Iterable[str] | None = None) -> str:
    """Replace every known secret value inside ``text`` with ``REDACTION``."""
    if not text:
        return text
    for secret in _current_secrets() if secrets is None else secrets:
        if secret in text:
            text = text.replace(secret, REDACTION)
    return text


class SecretRedactingFilter(logging.Filter):
    """Scrub secret values out of a record before a handler formats it.

    The record's message, its arguments and any formatted traceback are all
    checked. The record is never dropped -- redacting is always preferable to
    losing the log line.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = _current_secrets()
        if not secrets:
            return True

        try:
            message = record.getMessage()
        except Exception:
            # A broken format string is a different bug; do not mask it here.
            return True

        if any(secret in message for secret in secrets):
            # Collapse msg + args into one already-interpolated, scrubbed string.
            record.msg = redact(message, secrets)
            record.args = None

        if record.exc_info and not record.exc_text:
            try:
                text = _exception_formatter.formatException(record.exc_info)
            except Exception:  # pragma: no cover - defensive
                text = ""
            if text and any(secret in text for secret in secrets):
                record.exc_text = redact(text, secrets)

        if record.exc_text and any(secret in record.exc_text for secret in secrets):
            record.exc_text = redact(record.exc_text, secrets)

        if record.stack_info and any(secret in record.stack_info for secret in secrets):
            record.stack_info = redact(record.stack_info, secrets)

        return True


def setup_logging(
    level: str | int | None = None,
    log_file: Path | str | None = None,
    force: bool = False,
) -> logging.Logger:
    """Configure root logging for the whole application.

    Installs a stderr handler and a rotating file handler at
    ``state/logs/towbook.log``, both carrying SecretRedactingFilter.

    level     -- overrides the LOG_LEVEL environment variable (default INFO).
    log_file  -- overrides the default log file location. Pass False-y ``""``
                 to disable file logging entirely.
    force     -- reconfigure even if setup_logging has already run.

    Returns the root logger. Idempotent: calling it twice does not double up
    handlers, which matters because the CLI, the scheduler and the web app can
    each call it.
    """
    global _configured
    root = logging.getLogger()

    if _configured and not force:
        return root

    if level is None:
        level = os.environ.get("LOG_LEVEL", "INFO")
    if isinstance(level, str):
        resolved = logging.getLevelName(level.strip().upper())
        level = resolved if isinstance(resolved, int) else logging.INFO

    # Remove handlers we installed previously so force=True is genuinely a
    # reconfigure rather than an append.
    for handler in list(root.handlers):
        if getattr(handler, "_towbook_handler", False):
            root.removeHandler(handler)
            handler.close()

    root.setLevel(level)
    formatter = logging.Formatter(fmt=_DEFAULT_FORMAT, datefmt=_DEFAULT_DATEFMT)
    redaction_filter = SecretRedactingFilter()

    stream_handler = logging.StreamHandler(stream=sys.stderr)
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(redaction_filter)
    stream_handler._towbook_handler = True  # type: ignore[attr-defined]
    root.addHandler(stream_handler)

    target: Path | None
    if log_file is None:
        target = LOG_DIR / "towbook.log"
    elif log_file:
        target = Path(log_file)
    else:
        target = None

    if target is not None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                target,
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
                delay=True,
            )
            file_handler.setLevel(level)
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redaction_filter)
            file_handler._towbook_handler = True  # type: ignore[attr-defined]
            root.addHandler(file_handler)
        except OSError as exc:
            # A read-only or missing state directory must not stop the run;
            # stderr logging still works.
            root.warning("file logging disabled, could not open %s: %s", target, exc)

    # These libraries are chatty at DEBUG and leak request bodies at times.
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "apscheduler.executors.default"):
        logging.getLogger(noisy).setLevel(max(level, logging.INFO))

    _configured = True
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a module logger, configuring logging first if nobody has yet."""
    if not _configured:
        setup_logging()
    return logging.getLogger(name)

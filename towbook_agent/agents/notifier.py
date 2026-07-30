"""Delivery of reports and events to humans.

Routing is entirely data. ``config/notifications.yaml`` decides which report or
event goes to which role over which channel with which template; adding a route
is a config edit, not a code change (hard constraint #2).

Four things in this module are load bearing and should not be "simplified":

1. **The hourly SMS shape is exact.** It replaces a human texting these
   numbers, so it has to read identically to what was sent by hand::

       14:00-14:59 | Offered 12 / Accepted 9 (75%)
       Day: 84 / 61 (73%)

   A **third line is appended only when there is something to act on** --

       !! 3 tows unanswered this hour

   -- and is absent entirely when that count is zero. See
   :func:`missed_work_line`. The two documented lines never change shape, and
   the third never fires on a quiet hour, because a warning that arrives every
   hour is one the owner stops reading.

2. **Every report leads with missed work.** MISSED_WORK_MODEL.md section 7:
   the deliverable is the inventory of work we did *not* get, attributed to a
   cause, with an action attached. Acceptance rate is the supporting context,
   not the headline. :func:`missed_work_context` flattens that inventory into
   the placeholders the daily / weekly templates lead with.

   **Every ranked list says it is ranked by job count** (:data:`RANKING_NOTE`).
   ``offerAmount`` is empty on 100% of the records this account produces, so no
   dollar figure is derivable and none may be implied. A ranked list with no
   stated unit invites a reader to assume money, which is the one thing these
   numbers are not.

3. **pipeline_failure is NON-SUPPRESSIBLE.** It ignores quiet hours *and* it
   ignores the rate limit. A missing report has to be louder than a bad one,
   and silence is never treated as success (hard constraint #5).
   :func:`is_non_suppressible` exists so a test can assert this directly.

4. **Recipient contact details never live in YAML.** ``notifications.yaml``
   maps a role to the *name* of the environment variable holding the phone
   number or address; the value is read at send time (hard constraint #1).

Channels are small classes in the :data:`CHANNELS` registry, so a new transport
is one class plus one line of YAML.
"""

from __future__ import annotations

import logging
import os
import re
import smtplib
import threading
import time
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, time as _time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, TemplateNotFound, Undefined

from ..core.config_loader import get_notifications, rules_version
from ..core.db import get_session
from ..core.logging_setup import get_logger
from ..core import companies as _companies
from ..core.models import AlertFired, utcnow
from ..core.paths import CONFIG_DIR, PACKAGE_ROOT

try:  # Python 3.9+ stdlib; tzdata may still be missing on a bare Windows box.
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - 3.11+ always has it
    ZoneInfo = None  # type: ignore[assignment]

    class ZoneInfoNotFoundError(Exception):  # type: ignore[no-redef]
        pass


__all__ = [
    "dispatch_report",
    "dispatch_event",
    "build_context",
    "missed_work_context",
    "missed_work_line",
    "render_template",
    "render_source",
    "resolve_recipient",
    "in_quiet_hours",
    "is_non_suppressible",
    "evaluate_suppression",
    "Suppression",
    "RenderedMessage",
    "parse_duration",
    "format_percent",
    "local_timezone",
    "dry_run_enabled",
    "Channel",
    "SmsChannel",
    "EmailChannel",
    "WebhookChannel",
    "CHANNELS",
    "register_channel",
    "get_channel",
    "SendError",
    "TransientSendError",
    "PermanentSendError",
    "MissingCredentials",
    "NON_SUPPRESSIBLE_EVENTS",
    "DEFAULT_TIMEZONE",
    "RANKING_NOTE",
    "NO_REVENUE_NOTE",
    "RANKING_NOTE_FULL",
    "RANKING_NOTE_FULL_VALUED",
    "ESTIMATED_VALUE_NOTE",
    "ranking_note",
]

logger = get_logger(__name__)

#: Fallback when notifications.yaml carries no ``non_suppressible_events``.
#: pipeline_failure is in here as a floor, not as a default that config can
#: silently drop: see _non_suppressible_events().
NON_SUPPRESSIBLE_EVENTS: tuple[str, ...] = ("pipeline_failure",)

DEFAULT_TIMEZONE: str = "America/Detroit"

#: Where Jinja2 looks for file-backed templates (``body_template:`` in YAML).
#: config/templates wins so an operator can override a shipped template
#: without touching the package.
AGENT_TEMPLATE_DIR: Path = PACKAGE_ROOT / "agents" / "templates"
CONFIG_TEMPLATE_DIR: Path = CONFIG_DIR / "templates"

#: Indirection point so tests can run the retry path without really sleeping.
_sleep: Callable[[float], None] = time.sleep

#: Re-entrancy guard: a failed pipeline_failure send must not emit another
#: pipeline_failure, which would fail the same way, forever.
_local = threading.local()


# ==========================================================================
# Errors
# ==========================================================================


class SendError(RuntimeError):
    """A channel could not deliver a message."""


class TransientSendError(SendError):
    """Transport level failure (5xx, timeout). Worth retrying."""


class PermanentSendError(SendError):
    """Rejection (bad number, bad address). Retrying cannot help."""


class MissingCredentials(SendError):
    """The channel is not configured. Fall back to logging, never crash."""


# ==========================================================================
# Small helpers
# ==========================================================================


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def dry_run_enabled() -> bool:
    """True when DRY_RUN is set. Nothing is sent; bodies are logged instead."""
    return _truthy(os.environ.get("DRY_RUN"))


def local_timezone() -> Any:
    """Return the reporting timezone from TZ (default America/Detroit).

    Report windows and quiet hours are human-facing and therefore local. All
    *storage* stays naive UTC -- see core/models.py.
    """
    name = (os.environ.get("TZ") or "").strip() or DEFAULT_TIMEZONE
    if ZoneInfo is None:  # pragma: no cover - stdlib always present on 3.11+
        return timezone.utc
    for candidate in (name, DEFAULT_TIMEZONE):
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            logger.warning("unknown timezone %r; falling back", candidate)
    return timezone.utc


def _as_datetime(value: Any) -> datetime | None:
    """Coerce a stored/serialised timestamp into a datetime, or None."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, _date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def _to_local(value: Any) -> datetime | None:
    """Convert a naive-UTC or aware datetime into the reporting timezone."""
    moment = _as_datetime(value)
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(local_timezone())


def _already_local(value: Any) -> datetime | None:
    """Read a value that is ALREADY in the reporting timezone.

    The counterpart to :func:`_to_local`. A naive value is stamped with the
    local zone rather than shifted into it; an aware value is converted. Use
    this for anything a metrics document produced from ``start_local``.
    """
    moment = _as_datetime(value)
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=local_timezone())
    return moment.astimezone(local_timezone())


def _window_local(source: Any, *names: str) -> datetime | None:
    """A window boundary from a metrics document, as local time.

    Metrics documents carry each boundary TWICE and in two different clocks:
    ``window_start`` is ``start_local`` (local, naive, with ``timezone`` stated
    beside it in the same document) and ``window_start_utc`` is the same moment
    in UTC. Both are naive ISO strings, so nothing about the value itself says
    which clock it is on -- only its key does.

    Feeding the LOCAL one through :func:`_to_local`, which assumes naive means
    UTC, subtracts the UTC offset a second time. That is a silent four-hour
    error in Detroit summer and five in winter, and it lands on the hourly SMS
    -- the message this system sends most often and the one that replaced a
    person texting these numbers. The counts would be for 18:00 while the label
    read 14:00.

    So: prefer the explicitly-UTC key and convert it, and fall back to reading
    the plain key as already-local. Never guess from the value.
    """
    if not isinstance(source, dict):
        return None
    for name in names:
        utc_value = source.get(f"{name}_utc")
        if utc_value not in (None, ""):
            moment = _to_local(utc_value)
            if moment is not None:
                return moment
        local_value = source.get(name)
        if local_value not in (None, ""):
            moment = _already_local(local_value)
            if moment is not None:
                return moment
    return None


_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _as_date_text(value: Any, fmt: str = "%Y-%m-%d") -> str | None:
    """Format a calendar date for display.

    A date is already local -- metrics_daily.date is the local calendar date --
    so it must NOT go through the UTC to local conversion. Doing so turns
    "2026-07-27" into midnight UTC, which is 20:00 on the 26th in Detroit, and
    the daily report silently reports yesterday.
    """
    if isinstance(value, _date) and not isinstance(value, datetime):
        return value.strftime(fmt)
    if isinstance(value, str) and _DATE_ONLY.fullmatch(value.strip()):
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").strftime(fmt)
        except ValueError:  # pragma: no cover - regex already validated it
            pass
    moment = _to_local(value)
    return moment.strftime(fmt) if moment else None


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    number = _as_number(value)
    return int(round(number)) if number is not None else None


def _first(mapping: dict, *keys: str) -> Any:
    """Return the first present, non-None value among ``keys``."""
    for key in keys:
        if isinstance(mapping, dict) and mapping.get(key) is not None:
            return mapping[key]
    return None


def _counts(metrics: dict, nested_key: str) -> dict[str, Any]:
    """Scalar counters for a window, wherever that report keeps them.

    ``compute_hourly`` puts offered / accepted / rate flat at the top level
    because the hourly SMS needs them there, while ``compute_daily`` and
    ``compute_weekly`` keep the same numbers under ``totals``. Reading only the
    top level made every daily text read "Offered None / Accepted None (?%)".

    The nested block is the base and the top level is laid over it, so the
    flat hourly spelling still wins where both exist.
    """
    merged: dict[str, Any] = {}
    nested = metrics.get(nested_key)
    if isinstance(nested, dict):
        merged.update(nested)
    for key, value in metrics.items():
        # Scalars only: the structures (by_client, alerts, ...) are read
        # through their own helpers and must not shadow a counter.
        if value is not None and not isinstance(value, (dict, list)):
            merged[key] = value
    return merged


def parse_duration(text: Any) -> timedelta:
    """Parse ``"4h"``, ``"90m"``, ``"1h30m"``, ``"45"`` (seconds) -> timedelta.

    Used for ``rate_limit.same_alert_same_entity``. Unparseable input yields a
    zero duration, which disables the limit rather than silently inventing one.
    """
    if isinstance(text, timedelta):
        return text
    number = _as_number(text)
    if number is not None and not isinstance(text, str):
        return timedelta(seconds=float(number))

    raw = str(text or "").strip().lower()
    if not raw:
        return timedelta(0)
    if raw.isdigit():
        return timedelta(seconds=int(raw))

    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    total = 0.0
    matched = False
    for amount, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([smhdw])", raw):
        total += float(amount) * units[unit]
        matched = True
    if not matched:
        logger.warning("could not parse duration %r; treating as no limit", text)
        return timedelta(0)
    return timedelta(seconds=total)


def format_percent(
    value: Any,
    decimals: int = 0,
    rounding: str = "half_up",
) -> str:
    """Format a 0..100 percentage as a bare number, no ``%`` sign.

    The ``%`` lives in the template so the sign placement is data too. Rounding
    is half-up by default because that is what a human doing this by hand does;
    Python's default banker's rounding turns 72.5 into 72, which reads as a
    mistake to the person receiving the text.
    """
    number = _as_number(value)
    if number is None:
        return "?"
    try:
        quantum = Decimal(1).scaleb(-int(decimals))
        mode = ROUND_HALF_UP if str(rounding).lower().replace("-", "_") == "half_up" else None
        quantised = Decimal(str(number)).quantize(quantum, rounding=mode) if mode else Decimal(
            str(number)
        ).quantize(quantum)
    except (InvalidOperation, ValueError):
        return "?"
    text = f"{quantised:f}"
    return text


def _rate_percent(
    accepted: Any,
    offered: Any,
    rate: Any = None,
    *,
    formatting: dict | None = None,
) -> str:
    """Percentage for the ``{*_pct}`` placeholders.

    A window with zero offers renders as the configured
    ``zero_offers_rate_pct`` (0), never as a division error.
    """
    formatting = formatting or {}
    decimals = int(formatting.get("percent_decimals", 0) or 0)
    rounding = str(formatting.get("percent_rounding", "half_up"))

    offered_n = _as_number(offered)
    accepted_n = _as_number(accepted)

    if offered_n is not None and offered_n > 0 and accepted_n is not None:
        return format_percent(accepted_n / offered_n * 100.0, decimals, rounding)
    if offered_n is not None and offered_n == 0:
        return format_percent(formatting.get("zero_offers_rate_pct", 0), decimals, rounding)

    rate_n = _as_number(rate)
    if rate_n is not None:
        # Stored rate is a fraction (Numeric(6,4)); tolerate a caller that
        # already multiplied by 100.
        scaled = rate_n * 100.0 if rate_n <= 1.0 else rate_n
        return format_percent(scaled, decimals, rounding)
    return "?"


def _mask(value: str | None, channel: str) -> str:
    """Mask a recipient before it is written to alerts_fired.

    The delivery record is useful; a database full of customer phone numbers
    and mailbox addresses is a liability.
    """
    if not value:
        return ""
    if channel == "webhook" or "://" in value:
        match = re.match(r"^([a-z]+://[^/]+)", value, re.IGNORECASE)
        return (match.group(1) + "/...") if match else "***"
    if "@" in value:
        local_part, _, domain = value.partition("@")
        return f"{local_part[:1]}***@{domain}"
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 4:
        return f"***{digits[-4:]}"
    return "***"


# ==========================================================================
# Templating
# ==========================================================================


class _QuestionMarkUndefined(Undefined):
    """A missing placeholder renders as ``?`` instead of blowing up the send.

    A slightly wrong text still beats no text: the owner would rather read
    "Offered ? / Accepted 9" and go looking than receive nothing at all.
    """

    def __str__(self) -> str:  # noqa: D105
        return "?"

    def __html__(self) -> str:  # noqa: D105
        return "?"


#: ``{name}`` placeholders as shipped in notifications.yaml. Converted to
#: Jinja2 expressions so operators can keep the simple syntax while the
#: renderer stays a single engine.
_SIMPLE_PLACEHOLDER = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")


def _to_jinja(source: str) -> str:
    """Translate ``{name}`` into ``{{ name }}`` unless the source is already Jinja2."""
    if "{{" in source or "{%" in source:
        return source
    return _SIMPLE_PLACEHOLDER.sub(r"{{ \1 }}", source)


def _pct_filter(accepted: Any, offered: Any = None) -> str:
    """``{{ row.accepted | pct(row.offered) }}`` or ``{{ row.rate | pct }}``."""
    try:
        formatting = get_notifications().get("formatting") or {}
    except Exception:  # pragma: no cover - config unreadable
        formatting = {}
    if offered is None:
        return _rate_percent(None, None, accepted, formatting=formatting)
    return _rate_percent(accepted, offered, formatting=formatting)


def _num_filter(value: Any) -> str:
    """Render a count, with ``?`` for missing instead of an empty cell."""
    number = _as_int(value)
    return "?" if number is None else f"{number:,}"


def _make_environment(autoescape: bool) -> Environment:
    loaders = [
        FileSystemLoader(str(CONFIG_TEMPLATE_DIR)),  # operator override wins
        FileSystemLoader(str(AGENT_TEMPLATE_DIR)),
    ]
    env = Environment(
        loader=ChoiceLoader(loaders),
        undefined=_QuestionMarkUndefined,
        autoescape=autoescape,
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=False,
    )
    env.filters["pct"] = _pct_filter
    env.filters["num"] = _num_filter
    return env


#: Two environments on purpose. An SMS body must never be HTML-escaped -- an
#: "&" in a client name would arrive as "&amp;" on the owner's phone.
_TEXT_ENV = _make_environment(autoescape=False)
_HTML_ENV = _make_environment(autoescape=True)


def render_source(source: str, context: dict, *, html: bool = False) -> str:
    """Render a template string. Never raises; a broken template logs and degrades."""
    try:
        env = _HTML_ENV if html else _TEXT_ENV
        return env.from_string(_to_jinja(str(source))).render(**context)
    except Exception as exc:  # pragma: no cover - malformed operator template
        logger.error("template render failed (%s: %s)", type(exc).__name__, exc)
        return str(source)


@dataclass
class RenderedMessage:
    """One message ready for a channel."""

    template_name: str
    body: str
    subject: str = ""
    html: bool = False


#: Built-in fallbacks so a truncated or hand-edited notifications.yaml cannot
#: silence the system. config always wins when it defines the template.
_BUILTIN_TEMPLATES: dict[str, dict[str, Any]] = {
    # The two documented lines, verbatim, plus the one conditional line. The
    # `{% if %}` keeps the newline INSIDE the conditional so a quiet hour sends
    # exactly the two lines it always sent -- no trailing blank line, no "0".
    "hourly_short": {
        "channel": "sms",
        "body": "{{ hour_start }}-{{ hour_end }} | Offered {{ offered }} / "
        "Accepted {{ accepted }} ({{ rate_pct }}%)\n"
        "Day: {{ day_offered }} / {{ day_accepted }} ({{ day_rate_pct }}%)"
        "{% if missed_alert_line %}\n{{ missed_alert_line }}{% endif %}",
    },
    # Leads with what we did NOT get. Acceptance rate is the last line, as
    # supporting context (MISSED_WORK_MODEL.md section 7).
    "daily_summary": {
        "channel": "sms",
        "body": "{% if missed_available %}"
        "{{ date }} | MISSED {{ missed | num }} of {{ missed_offers | num }} "
        "({{ recoverable | num }} recoverable)\n"
        "Unanswered {{ no_response | num }} | Declined {{ declined | num }} | "
        "Withdrawn {{ withdrew | num }}\n"
        "{% if top_cause %}Top cause {{ top_cause }} {{ top_cause_missed | num }} "
        "-> {{ top_cause_remedy }}\n{% endif %}"
        "{% if blind_spot_labels %}Blind spots {{ blind_spot_labels }}\n{% endif %}"
        "{% if closeoff_summary %}Close off {{ closeoff_summary }}\n{% endif %}"
        "Accepted {{ accepted | num }}/{{ offered | num }} ({{ rate_pct }}%) "
        "- job counts, no $"
        "{% else %}"
        "{{ date }} | Offered {{ offered | num }} / Accepted {{ accepted | num }} "
        "({{ rate_pct }}%)\n"
        "Missed-work breakdown unavailable for this window - check the agent log."
        "{% endif %}",
    },
    # The subject line is the part that gets read on a locked screen, so it
    # carries the missed-work headline and not the acceptance rate.
    "daily_full": {
        "channel": "email",
        "subject": "{% if missed_available %}"
        "Towbook daily - {{ date }} - {{ missed | num }} missed, "
        "{{ recoverable | num }} recoverable ({{ accepted | num }}/{{ offered | num }} accepted)"
        "{% else %}"
        "Towbook daily - {{ date }} - {{ accepted | num }}/{{ offered | num }} "
        "accepted ({{ rate_pct }}%)"
        "{% endif %}",
        "body_template": "daily_full.html.j2",
    },
    "weekly_full": {
        "channel": "email",
        "subject": "{% if missed_available %}"
        "Towbook weekly - week of {{ week_start }} - {{ missed | num }} missed, "
        "{{ recoverable | num }} recoverable ({{ rate_pct }}% accepted)"
        "{% else %}"
        "Towbook weekly - week of {{ week_start }} - {{ rate_pct }}% acceptance"
        "{% endif %}",
        "body_template": "weekly_full.html.j2",
    },
    # The month's subject carries DIRECTION, not level: "412 missed" is the
    # weekly's job, and the month exists to say whether that is more or less
    # than last month.
    "monthly_full": {
        "channel": "email",
        "subject": "{% if missed_available %}"
        "Towbook monthly - {{ month_name }} - {{ missed | num }} missed vs "
        "{{ missed_prior | num }} in {{ prior_month_name }} "
        "({{ rate_pct }}% accepted)"
        "{% else %}"
        "Towbook monthly - {{ month_name }} - {{ rate_pct }}% acceptance"
        "{% endif %}",
        "body_template": "monthly_full.html.j2",
    },
    "alert_sms": {
        "channel": "sms",
        "body": "ALERT [{severity}] {alert_id}\n{entity}\n{detail}",
    },
    "pipeline_failure_sms": {
        "channel": "sms",
        "body": "TOWBOOK PIPELINE FAILURE\n{stage}: {error}\n"
        "Window {window_start} - {window_end}\n"
        "No data was collected. Check the agent.",
    },
}


def _template_spec(name: str, notifications: dict | None = None) -> dict[str, Any]:
    notifications = notifications if notifications is not None else get_notifications()
    templates = notifications.get("templates") or {}
    spec = templates.get(name)
    if isinstance(spec, str):  # a bare body string is a legal shorthand
        return {"body": spec}
    if isinstance(spec, dict):
        return spec
    fallback = _BUILTIN_TEMPLATES.get(name)
    if fallback is not None:
        logger.warning("template %r missing from notifications.yaml; using built-in", name)
        return dict(fallback)
    return {}


def render_template(
    name: str,
    context: dict,
    notifications: dict | None = None,
) -> RenderedMessage:
    """Render the named template from notifications.yaml (or the built-in)."""
    spec = _template_spec(name, notifications)
    notifications = notifications if notifications is not None else get_notifications()
    formatting = notifications.get("formatting") or {}

    subject = ""
    if spec.get("subject"):
        subject = render_source(spec["subject"], context, html=False)

    body_template = spec.get("body_template")
    html = False
    if body_template:
        filename = str(body_template)
        html = filename.endswith((".html", ".html.j2", ".htm.j2", ".htm"))
        env = _HTML_ENV if html else _TEXT_ENV
        try:
            body = env.get_template(filename).render(**context)
        except TemplateNotFound:
            logger.error(
                "template file %s not found under %s or %s; falling back to inline body",
                filename,
                CONFIG_TEMPLATE_DIR,
                AGENT_TEMPLATE_DIR,
            )
            body = render_source(spec.get("body") or _plaintext_fallback(context), context)
            html = False
        except Exception as exc:  # pragma: no cover - malformed operator template
            logger.error("template file %s failed to render: %s", filename, exc)
            body = render_source(spec.get("body") or _plaintext_fallback(context), context)
            html = False
    elif spec.get("body"):
        body = render_source(spec["body"], context, html=False)
    else:
        logger.error("template %r has neither body nor body_template", name)
        body = render_source(_plaintext_fallback(context), context)

    limit = _as_int(formatting.get("sms_max_length"))
    if limit and not html and len(body) > limit:
        # Truncate rather than let the carrier split it into several billed
        # messages with no indication that anything was cut.
        body = body[: max(0, limit - 3)].rstrip() + "..."

    return RenderedMessage(template_name=name, body=body, subject=subject, html=html)


def _plaintext_fallback(context: dict) -> str:
    """Last-resort body: say what we know rather than sending an empty text.

    Leads with missed work when the model ran, and falls back to the acceptance
    figures when it did not -- a degraded report still reports the right thing
    first.
    """
    parts = [
        "Towbook {report_type} report",
        "Offered {offered} / Accepted {accepted} ({rate_pct}%)",
    ]
    if context.get("missed_available"):
        parts = [
            "Towbook {report_type} report",
            "MISSED {missed} of {missed_offers} ({recoverable} recoverable)",
            "Accepted {accepted} ({rate_pct}%) - job counts, no $",
        ]
    if context.get("alert_id"):
        parts = ["ALERT [{severity}] {alert_id} {entity}"]
    if context.get("stage"):
        parts = ["TOWBOOK PIPELINE FAILURE", "{stage}: {error}"]
    return "\n".join(parts)


# ==========================================================================
# Context building
# ==========================================================================


def _service_class_stats(metrics: dict) -> dict[str, dict]:
    """Normalise whatever shape metrics.py used for per-service-class counts."""
    raw = _first(metrics, "by_service_class", "service_classes", "service_class") or {}
    result: dict[str, dict] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(value, dict):
                result[str(key)] = value
            else:
                result[str(key)] = {"offered": value}
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                key = _first(item, "service_class", "class", "name")
                if key:
                    result[str(key)] = item
    return result


def _client_rows(metrics: dict) -> list[dict]:
    raw = _first(metrics, "clients", "by_client", "client_daily") or []
    if isinstance(raw, dict):
        rows = []
        for key, value in raw.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("client_key", key)
                rows.append(row)
        return rows
    if isinstance(raw, list):
        return [row for row in raw if isinstance(row, dict)]
    return []


def _top_missed_client(metrics: dict) -> str:
    """The client that lost us the most jobs, for the daily SMS one-liner."""
    explicit = _first(metrics, "top_missed_client", "worst_client")
    if explicit is None:
        # compute_daily / compute_weekly publish the authoritative answer at
        # policy_variance.top_missed_client -- the client that cost us the most
        # jobs the acceptance policy says we should have taken. That is a
        # different (and better) question than "who had the most unaccepted
        # offers", which is all the fallback below can work out.
        variance = metrics.get("policy_variance")
        if isinstance(variance, dict):
            explicit = _first(variance, "top_missed_client", "worst_client")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    if isinstance(explicit, dict):
        name = _first(explicit, "client_name", "client", "client_key")
        if name:
            return str(name)

    best: tuple[int, float, str] | None = None
    for row in _client_rows(metrics):
        offered = _as_int(_first(row, "offered")) or 0
        accepted = _as_int(_first(row, "accepted")) or 0
        missed = offered - accepted
        if missed <= 0:
            continue
        rate = (accepted / offered) if offered else 0.0
        name = _first(row, "client_name", "client", "client_key") or "unknown"
        candidate = (missed, -rate, str(name))
        if best is None or candidate > best:
            best = candidate
    return best[2] if best else "none"


# ==========================================================================
# Missed work -- the headline of every report
# (MISSED_WORK_MODEL.md section 7, computed by agents/missed_work.py)
# ==========================================================================

#: Printed under every ranked list in every report.
#:
#: ``offerAmount`` is empty on 100% of the records this account produces --
#: 3,079 of 3,079 over 30 days -- so every ordering in every report is by JOB
#: COUNT. Saying so is not boilerplate: a ranked list with no stated unit
#: invites the reader to assume money, and money is the one thing these numbers
#: are not.
RANKING_NOTE: str = "Ranked by job count."

#: The other half of the same sentence, for the case where nobody has supplied
#: average job values.
#:
#: The provenance disclaimer never disappears -- see ESTIMATED_VALUE_NOTE. What
#: changes is only WHICH true sentence is shown, because "these are jobs, not
#: dollars" becomes false the moment the reports start carrying
#: ``missed_value``: the reader is looking at dollars, and telling them they are
#: not is worse than telling them nothing.
NO_REVENUE_NOTE: str = (
    "Towbook does not send offer amounts, so these are jobs, not dollars."
)

#: Shown instead once ``missed_work.job_value_by_client`` is populated. Still
#: says Towbook sends no amounts -- that fact does not change -- but is honest
#: that a dollar figure is now being displayed and where it came from.
ESTIMATED_VALUE_NOTE: str = (
    "Towbook sends no offer amounts; dollar figures are estimates from the "
    "average job values configured in rules.yaml -- gross, not margin."
)

#: What every ranked list is captioned with when no values are configured.
RANKING_NOTE_FULL: str = f"{RANKING_NOTE} {NO_REVENUE_NOTE}"

#: Same caption once values are configured.
RANKING_NOTE_FULL_VALUED: str = f"{RANKING_NOTE} {ESTIMATED_VALUE_NOTE}"


def ranking_note(revenue_available: bool) -> str:
    """Return the caption matching what the report actually shows.

    Both variants state that Towbook supplies no amounts. They differ only in
    whether the report in the reader's hands contains dollars.
    """
    return RANKING_NOTE_FULL_VALUED if revenue_available else RANKING_NOTE_FULL

#: What a service class is called in a sentence.
#:
#: The class names themselves are data (rules.yaml -> service_classes), and a
#: text reading "3 light_service unanswered" reads like a database error rather
#: than a message from a colleague. Overridable per class in
#: notifications.yaml -> formatting.service_class_labels, so a renamed class is
#: still a config edit; these are only what ships.
_DEFAULT_CLASS_LABELS: dict[str, str] = {
    "tow": "tow",
    "winch_out": "winch-out",
    "light_service": "light-service job",
    "unclassified": "unclassified job",
}

#: How a message names the window it is talking about. Keyed by report type.
_DEFAULT_WINDOW_PHRASES: dict[str, str] = {
    "hourly": "this hour",
    "daily": "today",
    "weekly": "this week",
    "monthly": "this month",
}

#: Longest ranked list an SMS one-liner will name before "+N more".
_SMS_LIST_ITEMS: int = 3


def _missed_document(metrics: dict) -> dict[str, Any]:
    """The missed-work document metrics.py embedded, or an empty mapping.

    Absent rather than empty on three legitimate paths: an older stored blob
    computed before the model existed, a run where agents/missed_work.py was
    unimportable, and a computation that raised and was caught. All three
    degrade to "no missed-work section", never to a wrong number -- which is why
    every caller below checks ``missed_available`` rather than trusting a zero.
    """
    document = metrics.get("missed_work")
    return document if isinstance(document, dict) else {}


def _wanted_classes(document: dict) -> list[str]:
    """Service classes in ``acceptance_policy.should_accept``, as the doc saw them.

    Read from the document rather than from rules.yaml so a report always
    describes the rules that actually produced it, even when it is re-rendered
    from a stored blob after somebody edited the policy.
    """
    meta = document.get("inventory_meta")
    if isinstance(meta, dict):
        names = meta.get("service_classes")
        if isinstance(names, list) and names:
            return [str(name) for name in names]
    # inventory_meta lists them only while restrict_to_should_accept is on.
    # closeoff carries the same set unconditionally, as its own mirror image.
    closeoff = document.get("closeoff_candidates")
    if isinstance(closeoff, dict):
        baseline = closeoff.get("wanted_baseline")
        if isinstance(baseline, dict):
            names = baseline.get("service_classes")
            if isinstance(names, list):
                return [str(name) for name in names]
    return []


def _unanswered_by_class(document: dict) -> dict[str, int]:
    """``service_class -> offers nobody answered``, for work we said we want.

    Counted off ``inventory[].buckets['no_response']`` rather than off a cause
    name. The cause for an unanswered offer is configured (`attention`, from
    rules.yaml -> missed_work.bucket_remedies) and could be renamed tomorrow;
    the bucket is the thing the model is defined on, so counting the bucket
    keeps this correct through a config edit it never sees.
    """
    wanted = set(_wanted_classes(document))
    counts: dict[str, int] = {}
    inventory = document.get("inventory")
    if not isinstance(inventory, list):
        return counts
    for row in inventory:
        if not isinstance(row, dict):
            continue
        name = str(row.get("service_class") or "")
        # An empty wanted set means the document could not tell us which
        # classes the owner wants. Counting everything would put a light-service
        # job the owner is happy to lose into a "we missed this" warning, so
        # count nothing and let the line stay silent.
        if not name or name not in wanted:
            continue
        buckets = row.get("buckets")
        count = _as_int((buckets or {}).get("no_response")) if isinstance(buckets, dict) else None
        if count:
            counts[name] = counts.get(name, 0) + count
    return {name: count for name, count in counts.items() if count > 0}


def _class_labels(formatting: dict | None = None) -> dict[str, str]:
    labels = dict(_DEFAULT_CLASS_LABELS)
    configured = (formatting or {}).get("service_class_labels")
    if isinstance(configured, dict):
        for key, value in configured.items():
            if value:
                labels[str(key)] = str(value)
    return labels


def _class_label(name: str, count: int, formatting: dict | None = None) -> str:
    """Pluralised human label for a service class. ``tow`` + 3 -> ``tows``."""
    label = _class_labels(formatting).get(str(name)) or str(name).replace("_", " ")
    if count == 1:
        return label
    return label if label.endswith("s") else f"{label}s"


def _window_phrase(report_type: str, formatting: dict | None = None) -> str:
    phrases = dict(_DEFAULT_WINDOW_PHRASES)
    configured = (formatting or {}).get("window_phrases")
    if isinstance(configured, dict):
        for key, value in configured.items():
            if value:
                phrases[str(key)] = str(value)
    return phrases.get(str(report_type), "in this window")


def missed_work_line(
    counts: dict[str, int],
    report_type: str,
    formatting: dict | None = None,
) -> str:
    """The one actionable line the hourly SMS appends -- or ``""``.

    ``"!! 3 tows unanswered this hour"``. Returns an empty string when the count
    is zero, and the template drops the line entirely rather than sending
    ``"!! 0 tows unanswered"``, which is noise dressed as a warning.

    When more than one wanted service class went unanswered the classes are not
    listed -- the total is reported against a neutral noun -- because an SMS
    that grows a clause per class stops being the thing it replaced.
    """
    formatting = formatting or {}
    total = sum(int(value) for value in counts.values() if value)
    if total <= 0:
        return ""
    prefix = str(formatting.get("missed_work_prefix", "!!")).strip()
    if len(counts) == 1:
        label = _class_label(next(iter(counts)), total, formatting)
    else:
        label = str(formatting.get("mixed_class_label", "wanted jobs"))
    phrase = _window_phrase(report_type, formatting)
    return " ".join(part for part in (prefix, str(total), label, "unanswered", phrase) if part)


def _cause_rows(document: dict) -> list[dict[str, Any]]:
    """``by_cause`` as a list, worst first, ranked by job count."""
    by_cause = document.get("by_cause")
    if not isinstance(by_cause, dict):
        return []
    rows = [dict(value) for value in by_cause.values() if isinstance(value, dict)]
    rows.sort(key=lambda row: (-(_as_int(row.get("missed")) or 0), str(row.get("cause") or "")))
    return rows


def _inventory_rows(document: dict) -> list[dict[str, Any]]:
    """The recoverable inventory, already ranked by job count by missed_work.py."""
    inventory = document.get("inventory")
    if not isinstance(inventory, list):
        return []
    rows = [dict(row) for row in inventory if isinstance(row, dict)]
    meta = document.get("inventory_meta")
    limit = _as_int((meta or {}).get("top_n")) if isinstance(meta, dict) else None
    return rows[:limit] if limit and limit > 0 else rows


def _missed_job_rows(document: dict) -> list[dict[str, Any]]:
    """The per-job lookup table, already capped and ordered by missed_work.py.

    One row per job we did not get, each carrying ``towbook_ref`` -- the
    Towbook job number when the offer became a job, the Digital Request id when
    it did not. It is the section the owner reads with Towbook open.
    """
    rows = document.get("missed_jobs")
    return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _blind_spot_rows(document: dict) -> list[dict[str, Any]]:
    spots = (document.get("blind_spots") or {}).get("blind_spots")
    return [dict(row) for row in spots if isinstance(row, dict)] if isinstance(spots, list) else []


def _closeoff_rows(document: dict, key: str) -> list[dict[str, Any]]:
    rows = (document.get("closeoff_candidates") or {}).get(key)
    return [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _summarise(items: Sequence[str], limit: int = _SMS_LIST_ITEMS) -> str:
    """``"a, b, c +4 more"`` -- a ranked list squeezed into one SMS line."""
    kept = [str(item) for item in items if str(item).strip()]
    if not kept:
        return ""
    head = ", ".join(kept[:limit])
    remaining = len(kept) - limit
    return f"{head} +{remaining} more" if remaining > 0 else head


def _trend_cause_rows(metrics: dict) -> list[dict[str, Any]]:
    """Per-cause week-over-week movement: growing, shrinking or flat.

    ``direction`` arrives from metrics.py already decided (``up`` / ``down`` /
    ``flat`` / ``new`` / ``gone``) so a template never has to work out what a
    negative delta means. Both numbers travel with it, because "equipment
    declines are up" without "62 this week against 41 last" is an impression,
    not a finding.
    """
    trend = metrics.get("missed_work_trend")
    if not isinstance(trend, dict):
        return []
    by_cause = trend.get("by_cause")
    if not isinstance(by_cause, dict):
        return []
    rows: list[dict[str, Any]] = []
    for value in by_cause.values():
        if not isinstance(value, dict):
            continue
        row = dict(value)
        now = _as_int(row.get("missed")) or 0
        was = _as_int(row.get("missed_prior")) or 0
        row["supporting_number"] = f"{now} this period vs {was} last"
        rows.append(row)
    rows.sort(key=lambda row: (-(_as_int(row.get("missed")) or 0), str(row.get("cause") or "")))
    return rows


def _duplicate_block(metrics: dict) -> dict[str, Any]:
    """The collapse report, from wherever this window's document carries it.

    ``compute_daily`` and friends put it at the top level; the missed-work
    document carries its own. They describe the same collapse over the same
    rows, so the first one found is used and the missed-work copy is the
    fallback for a metrics blob written before the rule existed.
    """
    block = metrics.get("duplicates")
    if isinstance(block, dict) and block:
        return dict(block)
    document = _missed_document(metrics)
    block = document.get("duplicates") if isinstance(document, dict) else None
    return dict(block) if isinstance(block, dict) else {}


def _duplicates_note(block: dict) -> str | None:
    """One sentence a report can print verbatim, or None when there is nothing.

    Names the biggest suppressed outcome, because "12 collapsed" is a curiosity
    and "12 collapsed, 9 of them declines" is the reason the decline count in
    this report is lower than the one in the portal.
    """
    suppressed = _as_int(block.get("suppressed")) or 0
    if not suppressed:
        return None
    clusters = _as_int(block.get("clusters")) or 0
    jobs = "job" if clusters == 1 else "jobs"
    offers = "offer" if suppressed == 1 else "offers"
    note = (
        f"{suppressed} repeat {offers} counted once: {clusters} {jobs} the client "
        f"asked for more than once inside the hour."
    )
    by_status = block.get("by_status")
    if isinstance(by_status, dict) and by_status:
        worst, count = max(by_status.items(), key=lambda item: (item[1], item[0]))
        note += f" {count} of them had already been recorded as {worst}."
    return note


def missed_work_context(
    report_type: str,
    metrics: dict,
    formatting: dict | None = None,
) -> dict[str, Any]:
    """Flatten the missed-work document into report placeholders.

    Returns the keys every template leads with. When the document is absent the
    counts come back as ``None`` and ``missed_available`` is False, so a
    template can omit the whole section instead of printing a confident row of
    zeros for a computation that never ran.
    """
    formatting = formatting or {}
    document = _missed_document(metrics)
    totals = document.get("totals") if isinstance(document.get("totals"), dict) else {}
    available = bool(document) and bool(totals)

    offers = _as_int(totals.get("offers"))
    missed = _as_int(totals.get("missed"))
    recoverable = _as_int(totals.get("recoverable"))
    unanswered_counts = _unanswered_by_class(document)
    causes = _cause_rows(document)
    blind_spots = _blind_spot_rows(document)
    closeoff_types = _closeoff_rows(document, "by_service_type")
    revenue_available = bool(document.get("revenue_available"))

    coverage = document.get("coverage") if isinstance(document.get("coverage"), dict) else {}
    coverage_outside = coverage.get("outside") if isinstance(coverage.get("outside"), dict) else {}
    coverage_inside = coverage.get("inside") if isinstance(coverage.get("inside"), dict) else {}
    coverage_contrast = (
        coverage.get("contrast") if isinstance(coverage.get("contrast"), dict) else {}
    )

    top_cause = causes[0] if causes else {}

    context: dict[str, Any] = {
        "missed_work": document or None,
        "missed_available": available,
        "missed_totals": totals or None,
        "missed_offers": offers,
        "missed": missed,
        "recoverable": recoverable,
        # BOTH numbers, always, whatever count_client_withdrew_as_recoverable
        # says -- so the setting can never hide one (MISSED_WORK_MODEL.md s1).
        "recoverable_excluding_withdrew": _as_int(
            totals.get("recoverable_excluding_withdrew")
        ),
        "recoverable_including_withdrew": _as_int(
            totals.get("recoverable_including_withdrew")
        ),
        "withdrew": _as_int(totals.get("withdrew")),
        "declined": _as_int(totals.get("declined")),
        "no_response": _as_int(totals.get("no_response")),
        "accept_failed": _as_int(totals.get("accept_failed")),
        "unknown_status": _as_int(totals.get("unknown_status")),
        "missed_pct": _rate_percent(
            missed, offers, totals.get("missed_rate"), formatting=formatting
        ),
        "recoverable_pct": _rate_percent(
            recoverable, offers, totals.get("recoverable_rate"), formatting=formatting
        ),
        "no_response_pct": _rate_percent(
            _as_int(totals.get("no_response")),
            offers,
            totals.get("no_response_rate"),
            formatting=formatting,
        ),
        # -- coverage: the headline recoverable figure ------------------------
        # Outside the staffed window is where essentially all of the
        # recoverable work sits, so that is the number a report leads with.
        # The inside figure ships with it and never without it: 61.7% only
        # means something next to 5.8%, and the contrast is the business case.
        "coverage_available": bool(coverage.get("windows")),
        "coverage_label": str(coverage.get("default_label") or "") or None,
        "coverage_windows": _summarise(
            [
                str(window.get("label") or "")
                for window in coverage.get("definitions") or []
            ]
        ),
        "recoverable_uncovered": _as_int(coverage_outside.get("recoverable")),
        "recoverable_covered": _as_int(coverage_inside.get("recoverable")),
        "no_response_uncovered": _as_int(coverage_outside.get("no_response")),
        "no_response_covered": _as_int(coverage_inside.get("no_response")),
        "offers_uncovered": _as_int(coverage_outside.get("offers")),
        "offers_covered": _as_int(coverage_inside.get("offers")),
        "no_response_pct_uncovered": _rate_percent(
            _as_int(coverage_outside.get("no_response")),
            _as_int(coverage_outside.get("offers")),
            coverage_outside.get("no_response_rate"),
            formatting=formatting,
        ),
        "no_response_pct_covered": _rate_percent(
            _as_int(coverage_inside.get("no_response")),
            _as_int(coverage_inside.get("offers")),
            coverage_inside.get("no_response_rate"),
            formatting=formatting,
        ),
        "coverage_summary": str(coverage_contrast.get("summary") or "") or None,
        "coverage_multiple": coverage_contrast.get("multiple"),
        "coverage_finding": str(coverage.get("finding") or "") or None,
        # -- cause breakdown -------------------------------------------------
        "missed_causes": causes,
        "top_cause": str(top_cause.get("cause") or "") or None,
        "top_cause_missed": _as_int(top_cause.get("missed")),
        "top_cause_remedy": str(top_cause.get("remedy") or "") or None,
        "top_cause_question": str(top_cause.get("question") or "") or None,
        # -- the inventory ---------------------------------------------------
        "missed_inventory": _inventory_rows(document),
        "missed_inventory_meta": document.get("inventory_meta") or None,
        # -- the job list, one row per job we did not get ----------------------
        # The only row-level section in the report. It is what makes the report
        # actionable job by job: every row carries the reference that finds the
        # offer in Towbook.
        "missed_jobs": _missed_job_rows(document),
        "missed_jobs_meta": document.get("missed_jobs_meta") or None,
        # -- blind spots -----------------------------------------------------
        "blind_spots": blind_spots,
        "blind_spot_count": len(blind_spots),
        "blind_spot_labels": _summarise(
            [str(row.get("label") or "") for row in blind_spots]
        ),
        "blind_spot_note": str(
            (document.get("blind_spots") or {}).get("response_window_note") or ""
        )
        or None,
        # -- close-off candidates --------------------------------------------
        "closeoff_types": closeoff_types,
        "closeoff_clients": _closeoff_rows(document, "clients"),
        # Two, not three: this is the longest single line in the daily SMS and
        # the message has a hard 320-character cap. The full list is in the
        # email; the text only has to be enough to start the conversation.
        "closeoff_summary": _summarise(
            [
                f"{row.get('service_type_raw')} "
                f"{_as_int(row.get('offers')) or 0}/{_as_int(row.get('accepted')) or 0}"
                for row in closeoff_types
            ],
            limit=2,
        ),
        "closeoff_rationale": str(
            (document.get("closeoff_candidates") or {}).get("rationale") or ""
        )
        or None,
        # -- client comparison -----------------------------------------------
        "client_gap_findings": [
            row
            for row in ((document.get("client_comparison") or {}).get("findings") or [])
            if isinstance(row, dict)
        ],
        "client_comparison_rows": [
            row
            for row in ((document.get("client_comparison") or {}).get("clients") or [])
            if isinstance(row, dict)
        ],
        # -- the SMS one-liner ------------------------------------------------
        "unanswered_by_class": unanswered_counts,
        "unanswered_wanted": sum(unanswered_counts.values()),
        "missed_alert_line": missed_work_line(unanswered_counts, report_type, formatting),
        # -- how to read every number above -----------------------------------
        "ranking_basis": str(document.get("ranking_basis") or "job_count"),
        # True once a human put average job values in rules.yaml. Selects which
        # caption is accurate: both say Towbook sends no amounts, but only one
        # claims the reader is not looking at dollars -- and that claim is false
        # as soon as the report carries missed_value.
        "revenue_available": revenue_available,
        "ranking_note": ranking_note(revenue_available),
        "revenue_note": str(document.get("revenue_note") or "") or None,
        # -- weekly only --------------------------------------------------------
        "missed_trend": metrics.get("missed_work_trend")
        if isinstance(metrics.get("missed_work_trend"), dict)
        else None,
        "missed_trend_causes": _trend_cause_rows(metrics),
    }
    return context


def build_context(
    report_type: str,
    metrics: dict | None,
    analysis: dict | None = None,
    notifications: dict | None = None,
) -> dict[str, Any]:
    """Flatten a metrics object into the placeholders the templates use.

    Deliberately tolerant about key names: metrics.py may store
    ``day_running_offered`` (the column name) while a caller passes
    ``day_offered``. A notifier that only works with one spelling is a notifier
    that goes silent after an unrelated refactor.
    """
    notifications = notifications if notifications is not None else get_notifications()
    formatting = notifications.get("formatting") or {}
    time_format = str(formatting.get("time_format") or "%H:%M")
    date_format = str(formatting.get("date_format") or "%Y-%m-%d")

    metrics = dict(metrics or {})
    analysis = dict(analysis or {})

    window_counts = _counts(metrics, "totals")
    day_counts = _counts(metrics, "day_totals")

    offered = _as_int(_first(window_counts, "offered", "offered_count", "total_offered"))
    accepted = _as_int(_first(window_counts, "accepted", "accepted_count", "total_accepted"))
    rate = _first(window_counts, "rate", "acceptance_rate")

    day_offered = _as_int(_first(day_counts, "day_running_offered", "day_offered", "offered"))
    day_accepted = _as_int(_first(day_counts, "day_running_accepted", "day_accepted", "accepted"))
    day_rate = _first(day_counts, "day_running_rate", "day_rate", "acceptance_rate")

    context: dict[str, Any] = {
        "report_type": report_type,
        "metrics": metrics,
        "analysis": analysis,
        "offered": offered,
        "accepted": accepted,
        "denied": _as_int(_first(window_counts, "denied")),
        "expired": _as_int(_first(window_counts, "expired")),
        "canceled": _as_int(_first(window_counts, "canceled")),
        "pending": _as_int(_first(window_counts, "pending")),
        "rate": rate,
        "rate_pct": _rate_percent(accepted, offered, rate, formatting=formatting),
        "day_offered": day_offered,
        "day_accepted": day_accepted,
        "day_rate_pct": _rate_percent(day_accepted, day_offered, day_rate, formatting=formatting),
        "clients": _client_rows(metrics),
        "service_classes": _service_class_stats(metrics),
        "top_missed_client": _top_missed_client(metrics),
        "generated_at": datetime.now(local_timezone()).strftime(f"{date_format} {time_format}"),
        "narrative": analysis.get("narrative"),
        "clients_needing_attention": analysis.get("clients_needing_attention") or [],
        "trend_statements": analysis.get("trend_statements") or [],
        "proposed_rules": analysis.get("proposed_rules") or [],
        "llm": analysis.get("llm"),
    }

    # -- THE HEADLINE ---------------------------------------------------------
    # Merged after the acceptance figures rather than before them so that a
    # missed-work key can never be shadowed by a same-named status counter:
    # `declined` (the missed-work bucket, which excludes client withdrawals) and
    # `denied` (the canonical status, which historically did not) are two
    # different numbers, and the report must lead with the first.
    context.update(missed_work_context(report_type, metrics, formatting))

    # -- what this report did not count twice ---------------------------------
    # A motor club that gets no answer asks again, and the repeat is collapsed
    # before anything above is counted. Every number in the report is smaller
    # for it, so the report says so out loud: an owner who reconciles a total
    # against the portal and finds it 8% short must be able to see why from the
    # report itself. See agents/duplicates.py.
    duplicates = _duplicate_block(metrics)
    context["duplicates"] = duplicates
    context["duplicates_suppressed"] = _as_int(duplicates.get("suppressed")) or 0
    context["duplicate_clusters"] = _as_int(duplicates.get("clusters")) or 0
    context["duplicates_note"] = _duplicates_note(duplicates)

    # The Analyst's own missed-work answers, alongside the computed inventory.
    # Absent keys render as empty lists so a degraded (or LLM-free) analysis
    # simply drops those sections rather than breaking the send.
    context["missed_work_ranked"] = analysis.get("missed_work_ranked") or []
    context["cause_assessments"] = analysis.get("cause_assessments") or []
    context["close_off_requests"] = analysis.get("close_off_requests") or []

    # -- per service class shortcuts used by daily_summary --------------------
    for key, stats in context["service_classes"].items():
        prefix = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
        context[f"{prefix}_offered"] = _as_int(_first(stats, "offered"))
        context[f"{prefix}_accepted"] = _as_int(_first(stats, "accepted"))
    # The shipped daily SMS says "Light"; the classifier calls the class
    # light_service. Alias rather than rename, so rules.yaml stays the source
    # of truth for class names.
    for short, full in (("light", "light_service"), ("winch", "winch_out")):
        for suffix in ("offered", "accepted"):
            if context.get(f"{short}_{suffix}") is None:
                context[f"{short}_{suffix}"] = context.get(f"{full}_{suffix}")

    # -- window labels --------------------------------------------------------
    # NOT _to_local: compute_hourly emits `window_start` from `start_local`, so
    # it is already in the reporting timezone. See _window_local.
    window_start = _window_local(metrics, "window_start", "hour", "hour_start")
    if window_start is not None:
        # 14:00-14:59, not 14:00-15:00: the hour is inclusive of :59.
        window_end = window_start + timedelta(hours=1) - timedelta(minutes=1)
        context["hour_start"] = window_start.strftime(time_format)
        context["hour_end"] = window_end.strftime(time_format)
        context["window_start"] = window_start.strftime(f"{date_format} {time_format}")
        context["window_end"] = window_end.strftime(f"{date_format} {time_format}")
        context.setdefault("date", window_start.strftime(date_format))

    explicit_date = _as_date_text(_first(metrics, "date", "day"), date_format)
    if explicit_date:
        context["date"] = explicit_date

    week_start = _as_date_text(_first(metrics, "week_start", "week"), date_format)
    if week_start:
        context["week_start"] = week_start
        week_dt = _as_datetime(_first(metrics, "week_start", "week"))
        if week_dt is not None:
            context.setdefault(
                "week_end", (week_dt + timedelta(days=6)).strftime(date_format)
            )

    # -- monthly ---------------------------------------------------------------
    # compute_monthly already emits these as strings; they are copied through
    # rather than re-derived so the email cannot name a different month from the
    # one the numbers came from.
    for key in (
        "month_start",
        "month_end",
        "month_label",
        "month_name",
        "prior_month_start",
        "prior_month_end",
        "prior_month_label",
        "prior_month_name",
        "days_in_month",
        "offers_per_day",
        "prior_offers_per_day",
        "offers_per_day_delta",
    ):
        if metrics.get(key) is not None:
            context[key] = metrics[key]

    # The month-over-month headline the subject line leads with. Read off the
    # PRIOR missed-work document rather than recomputed, so "412 against 517"
    # is two numbers from two documents built by the same model.
    prior_document = metrics.get("prior_missed_work")
    prior_totals = (
        prior_document.get("totals") if isinstance(prior_document, dict) else None
    )
    if isinstance(prior_totals, dict):
        context["missed_prior"] = _as_int(prior_totals.get("missed"))
        context["missed_offers_prior"] = _as_int(prior_totals.get("offers"))
        context["recoverable_prior"] = _as_int(prior_totals.get("recoverable"))
        context["no_response_prior"] = _as_int(prior_totals.get("no_response"))
        context["withdrew_prior"] = _as_int(prior_totals.get("withdrew"))

    # Gated on the nested block itself: _counts() lays every top-level scalar
    # over it, so it is never empty and "did this report carry a prior period"
    # has to be asked of the block.
    if isinstance(metrics.get("prior_totals"), dict):
        prior_counts = _counts(metrics, "prior_totals")
        prior_offered = _as_int(_first(prior_counts, "offered"))
        prior_accepted = _as_int(_first(prior_counts, "accepted"))
        context["prior_offered"] = prior_offered
        context["prior_accepted"] = prior_accepted
        context["prior_rate_pct"] = _rate_percent(
            prior_accepted,
            prior_offered,
            _first(prior_counts, "rate", "acceptance_rate"),
            formatting=formatting,
        )

    for key in ("coverage_trend", "weekly_distribution", "client_trend"):
        value = metrics.get(key)
        if isinstance(value, list):
            context[key] = value
    if isinstance(metrics.get("outliers"), dict):
        context["outliers"] = metrics["outliers"]
    if isinstance(metrics.get("totals_delta"), dict):
        context["totals_delta"] = metrics["totals_delta"]

    context.setdefault("rules_version", _rules_version())
    return context


def _event_context(
    event_type: str,
    payload: dict,
    notifications: dict | None = None,
) -> dict[str, Any]:
    """Placeholders for alert_sms / pipeline_failure_sms."""
    notifications = notifications if notifications is not None else get_notifications()
    formatting = notifications.get("formatting") or {}
    date_format = str(formatting.get("date_format") or "%Y-%m-%d")
    time_format = str(formatting.get("time_format") or "%H:%M")
    stamp = f"{date_format} {time_format}"

    payload = dict(payload or {})
    context: dict[str, Any] = dict(payload)
    context.update(
        {
            "event_type": event_type,
            "alert_id": payload.get("alert_id") or event_type,
            "severity": payload.get("severity") or "high",
            "entity": payload.get("entity") or "",
            "stage": payload.get("stage") or "",
            "error": payload.get("error") or "",
            "rules_version": payload.get("rules_version") or _rules_version(),
        }
    )
    # Alert payloads are built by agents/metrics.py from `start_local` /
    # `end_local` (see the alert context around metrics.py `window_start`), so
    # these boundaries are local already and must not be shifted again.
    for key in ("window_start", "window_end"):
        moment = _window_local(payload, key)
        context[key] = moment.strftime(stamp) if moment else (payload.get(key) or "unknown")

    if not payload.get("detail"):
        skip = {
            "alert_id",
            "severity",
            "entity",
            "event_type",
            "emitted_at",
            "detail",
            "stage",
            "error",
            "window_start",
            "window_end",
            "rules_version",
        }
        extras = [
            f"{key}={value}"
            for key, value in payload.items()
            if key not in skip and value not in (None, "", [], {})
        ]
        context["detail"] = " ".join(extras[:6])
    return context


def _rules_version() -> str:
    try:
        return rules_version()
    except Exception:  # pragma: no cover - config unreadable
        return "unknown"


# ==========================================================================
# Recipients
# ==========================================================================


def resolve_recipient(
    role: str,
    channel: str,
    notifications: dict | None = None,
) -> str | None:
    """Resolve a role to a contact value via the env var named in YAML.

    Hard constraint #1. ``notifications.yaml`` holds ``phone_env: OWNER_PHONE``;
    the phone number itself only ever exists in the environment.
    """
    notifications = notifications if notifications is not None else get_notifications()
    recipients = notifications.get("recipients") or {}
    entry = recipients.get(role)
    if not isinstance(entry, dict):
        logger.error("notifications.yaml has no recipients entry for role %r", role)
        return None

    channel_cls = CHANNELS.get(channel)
    env_key_name = getattr(channel_cls, "recipient_env_key", None) or f"{channel}_env"
    env_name = entry.get(env_key_name)
    if not env_name:
        logger.error(
            "recipients.%s has no %s, so it cannot be reached over %s",
            role,
            env_key_name,
            channel,
        )
        return None

    value = (os.environ.get(str(env_name)) or "").strip()
    if not value:
        logger.warning(
            "environment variable %s is unset; cannot reach %r over %s",
            env_name,
            role,
            channel,
        )
        return None
    return value


# ==========================================================================
# Channels -- one class per transport, registered in a dict.
# A new channel is one class plus one line in notifications.yaml.
# ==========================================================================


CHANNELS: dict[str, type["Channel"]] = {}


def register_channel(cls: type["Channel"]) -> type["Channel"]:
    CHANNELS[cls.name] = cls
    return cls


def get_channel(name: str) -> "Channel | None":
    cls = CHANNELS.get(name)
    if cls is None:
        logger.error("unknown channel %r (known: %s)", name, ", ".join(sorted(CHANNELS)))
        return None
    return cls()


class Channel:
    """Base transport."""

    name: str = ""
    #: Key inside ``recipients.<role>`` naming the env var with the address.
    recipient_env_key: str = ""

    def available(self) -> tuple[bool, str]:
        """(configured?, human readable reason when not)."""
        return True, ""

    def send(self, *, recipient: str, message: RenderedMessage, context: dict) -> dict:
        raise NotImplementedError


@register_channel
class SmsChannel(Channel):
    """Twilio SMS."""

    name = "sms"
    recipient_env_key = "phone_env"

    def available(self) -> tuple[bool, str]:
        missing = [
            key
            for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM")
            if not (os.environ.get(key) or "").strip()
        ]
        if missing:
            return False, "missing " + ", ".join(missing)
        return True, ""

    def send(self, *, recipient: str, message: RenderedMessage, context: dict) -> dict:
        ok, reason = self.available()
        if not ok:
            raise MissingCredentials(f"sms channel not configured: {reason}")
        try:
            from twilio.base.exceptions import TwilioRestException
            from twilio.rest import Client
        except ImportError as exc:  # pragma: no cover - dependency listed in requirements
            raise MissingCredentials(f"twilio not installed: {exc}") from exc

        client = Client(
            os.environ["TWILIO_ACCOUNT_SID"].strip(),
            os.environ["TWILIO_AUTH_TOKEN"].strip(),
        )
        try:
            sent = client.messages.create(
                body=message.body,
                from_=os.environ["TWILIO_FROM"].strip(),
                to=recipient,
            )
        except TwilioRestException as exc:
            status = getattr(exc, "status", 0) or 0
            if 400 <= status < 500:
                # An invalid number is permanent. Retrying just burns time
                # while the owner waits for a text that will never arrive.
                raise PermanentSendError(f"twilio rejected the message: {exc}") from exc
            raise TransientSendError(f"twilio transport failure: {exc}") from exc
        except Exception as exc:
            raise TransientSendError(f"twilio call failed: {exc}") from exc
        return {"provider": "twilio", "sid": getattr(sent, "sid", None), "status": getattr(sent, "status", None)}


@register_channel
class EmailChannel(Channel):
    """SMTP email."""

    name = "email"
    recipient_env_key = "email_env"

    def available(self) -> tuple[bool, str]:
        if not (os.environ.get("SMTP_HOST") or "").strip():
            return False, "missing SMTP_HOST"
        if not (os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER") or "").strip():
            return False, "missing SMTP_FROM (or SMTP_USER)"
        return True, ""

    def send(self, *, recipient: str, message: RenderedMessage, context: dict) -> dict:
        ok, reason = self.available()
        if not ok:
            raise MissingCredentials(f"email channel not configured: {reason}")

        host = os.environ["SMTP_HOST"].strip()
        port = _as_int(os.environ.get("SMTP_PORT")) or 587
        user = (os.environ.get("SMTP_USER") or "").strip()
        password = os.environ.get("SMTP_PASS") or ""
        sender = (os.environ.get("SMTP_FROM") or user).strip()

        email = EmailMessage()
        email["Subject"] = message.subject or "Towbook report"
        email["From"] = sender
        email["To"] = recipient
        if message.html:
            email.set_content(_html_to_text(message.body))
            email.add_alternative(message.body, subtype="html")
        else:
            email.set_content(message.body)

        try:
            if port == 465:
                server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
            with server:
                server.ehlo()
                if port != 465:
                    try:
                        server.starttls()
                        server.ehlo()
                    except smtplib.SMTPException:
                        logger.warning("SMTP server %s:%s does not support STARTTLS", host, port)
                if user and password:
                    server.login(user, password)
                server.send_message(email)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused) as exc:
            raise PermanentSendError(f"smtp rejected the message: {exc}") from exc
        except smtplib.SMTPAuthenticationError as exc:
            raise PermanentSendError(f"smtp authentication failed: {exc}") from exc
        except Exception as exc:
            raise TransientSendError(f"smtp transport failure: {exc}") from exc
        return {"provider": "smtp", "host": host, "port": port}


@register_channel
class WebhookChannel(Channel):
    """HTTP POST. Present so adding Slack/Teams/PagerDuty is a config edit."""

    name = "webhook"
    recipient_env_key = "webhook_env"

    def available(self) -> tuple[bool, str]:
        return True, ""

    def send(self, *, recipient: str, message: RenderedMessage, context: dict) -> dict:
        if not recipient:
            raise MissingCredentials("webhook channel has no target URL")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise MissingCredentials(f"httpx not installed: {exc}") from exc

        body = {
            "template": message.template_name,
            "subject": message.subject,
            "text": message.body,
            "report_type": context.get("report_type"),
            "event_type": context.get("event_type"),
            "alert_id": context.get("alert_id"),
            "severity": context.get("severity"),
        }
        try:
            response = httpx.post(recipient, json=body, timeout=20.0)
        except Exception as exc:
            raise TransientSendError(f"webhook request failed: {exc}") from exc
        if 400 <= response.status_code < 500:
            raise PermanentSendError(
                f"webhook rejected the message with HTTP {response.status_code}"
            )
        if response.status_code >= 500:
            raise TransientSendError(f"webhook returned HTTP {response.status_code}")
        return {"provider": "webhook", "status_code": response.status_code}


def _html_to_text(html: str) -> str:
    """Crude HTML -> text for the multipart/alternative plain part."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|h[1-6]|li)>", "\n", text)
    text = re.sub(r"(?i)</t[dh]>", "\t", text)
    text = re.sub(r"<[^>]+>", "", text)
    # The typographic entities matter as much as the syntactic ones here: this
    # is the text/plain alternative every phone mail client shows in a preview,
    # and "Towbook daily &mdash; 2026-07-27" reads like a broken template.
    for entity, character in (
        ("&nbsp;", " "),
        ("&mdash;", "--"),
        ("&ndash;", "-"),
        ("&middot;", "-"),
        ("&hellip;", "..."),
        ("&rsquo;", "'"),
        ("&lsquo;", "'"),
        ("&ldquo;", '"'),
        ("&rdquo;", '"'),
        ("&#39;", "'"),
        ("&quot;", '"'),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&amp;", "&"),  # LAST: "&amp;mdash;" must not become an em dash
    ):
        text = text.replace(entity, character)
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


# ==========================================================================
# Suppression: quiet hours, rate limit, and the non-suppressible floor
# ==========================================================================


@dataclass(frozen=True)
class Suppression:
    """Why (or why not) a message is being held back.

    ``non_suppressible`` is the test-visible flag from hard constraint #5: a
    pipeline_failure carries it and is therefore never quiet-hour'd and never
    rate limited.
    """

    suppressed: bool = False
    reason: str | None = None
    non_suppressible: bool = False


def _non_suppressible_events(notifications: dict | None = None) -> set[str]:
    """Config may *add* to the non-suppressible set. It cannot remove
    pipeline_failure: an operator who quiets the failure alarm has quietly
    turned the whole system into something that fails silently."""
    notifications = notifications if notifications is not None else get_notifications()
    configured = notifications.get("non_suppressible_events") or []
    if isinstance(configured, str):
        configured = [configured]
    return {str(item) for item in configured} | set(NON_SUPPRESSIBLE_EVENTS)


def is_non_suppressible(event_type: str | None, notifications: dict | None = None) -> bool:
    """True when this event ignores quiet hours and the rate limit entirely."""
    if not event_type:
        return False
    return str(event_type) in _non_suppressible_events(notifications)


def _parse_clock(value: Any) -> _time | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M", "%H:%M:%S", "%H"):
        try:
            parsed = datetime.strptime(text, fmt)
            return _time(parsed.hour, parsed.minute, parsed.second)
        except ValueError:
            continue
    logger.warning("could not parse quiet hours clock value %r", value)
    return None


def in_quiet_hours(moment: datetime | None = None, notifications: dict | None = None) -> bool:
    """Is ``moment`` (local time) inside the configured quiet window?

    Handles a window that crosses midnight -- 21:00 to 06:00 is the shipped
    default, and a naive ``start <= t < end`` test would say "never".
    """
    notifications = notifications if notifications is not None else get_notifications()
    quiet = notifications.get("quiet_hours") or {}
    start = _parse_clock(quiet.get("start"))
    end = _parse_clock(quiet.get("end"))
    if start is None or end is None:
        return False

    if moment is None:
        moment = datetime.now(local_timezone())
    elif moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc).astimezone(local_timezone())
    else:
        moment = moment.astimezone(local_timezone())

    now = moment.timetz().replace(tzinfo=None)
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end  # window wraps past midnight


def _suppressed_severities(notifications: dict | None = None) -> set[str]:
    notifications = notifications if notifications is not None else get_notifications()
    quiet = notifications.get("quiet_hours") or {}
    values = quiet.get("suppress_severities") or []
    if isinstance(values, str):
        values = [values]
    return {str(value).strip().lower() for value in values}


def _rate_limit_window(notifications: dict | None = None) -> timedelta:
    notifications = notifications if notifications is not None else get_notifications()
    limits = notifications.get("rate_limit") or {}
    return parse_duration(limits.get("same_alert_same_entity"))


def _company_for(payload: dict | None = None, company_id: str | None = None) -> str:
    """Which company a message is about.

    The explicit argument first, then the payload -- metrics puts
    ``company_id`` on every alert it emits, and the pipeline puts it on every
    failure event -- then the company currently active. Never blank: an
    ``alerts_fired`` row that belongs to nobody cannot be rate limited, shown
    on a dashboard, or answered.
    """
    if company_id:
        return _companies.resolve_company_id(company_id)
    if isinstance(payload, dict):
        for key in ("company_id", "account_id"):
            value = payload.get(key)
            if value:
                return _companies.resolve_company_id(str(value))
    return _companies.resolve_company_id(None)


def _recently_delivered(
    alert_id: str, entity: str, window: timedelta, company_id: str | None = None
) -> datetime | None:
    """Last *delivered* firing of this alert for this entity inside the window.

    Suppressed rows are skipped on purpose: a message that was held back never
    reached anybody, so it must not start the cooldown on the next one.

    Scoped to one company. Two tenants can both have an entity of
    ``"agero (swoop)"``, and one company's delivered alert must not put the
    other company's identical alert into cooldown.
    """
    if window <= timedelta(0):
        return None
    cutoff = utcnow() - window
    try:
        with get_session(commit=False) as session:
            row = (
                session.query(AlertFired)
                .filter(
                    AlertFired.company_id == _company_for(None, company_id),
                    AlertFired.alert_id == alert_id,
                    AlertFired.entity == (entity or ""),
                    AlertFired.fired_at >= cutoff,
                    AlertFired.suppressed_reason.is_(None),
                )
                .order_by(AlertFired.fired_at.desc())
                .first()
            )
            return row.fired_at if row else None
    except Exception as exc:  # pragma: no cover - DB unavailable
        # Fail open. A rate limit that cannot be checked must not become an
        # excuse to drop the alert.
        logger.warning("rate limit check failed (%s); delivering anyway", exc)
        return None


def evaluate_suppression(
    *,
    event_type: str | None = None,
    alert_id: str | None = None,
    entity: str = "",
    severity: str | None = None,
    moment: datetime | None = None,
    notifications: dict | None = None,
    company_id: str | None = None,
) -> Suppression:
    """Decide whether this message may be delivered right now."""
    notifications = notifications if notifications is not None else get_notifications()

    if is_non_suppressible(event_type, notifications):
        return Suppression(suppressed=False, reason=None, non_suppressible=True)

    severity_key = str(severity or "").strip().lower()
    if severity_key and severity_key in _suppressed_severities(notifications):
        if in_quiet_hours(moment, notifications):
            return Suppression(True, "quiet_hours", False)

    if alert_id:
        window = _rate_limit_window(notifications)
        last = _recently_delivered(alert_id, entity, window, company_id)
        if last is not None:
            return Suppression(True, "rate_limit", False)

    return Suppression(False, None, False)


# ==========================================================================
# Recording
# ==========================================================================


def _record(
    *,
    alert_id: str,
    entity: str,
    severity: str | None,
    payload: dict,
    suppressed_reason: str | None = None,
    company_id: str | None = None,
) -> None:
    """Write the delivery (or the suppression) to alerts_fired.

    Stamped with the company the alert is about, taken from the payload when
    the emitter put one there. That column is what scopes the rate limit and
    what lets /health show one company's alert history without the others'.

    Best effort: a database problem must not stop a text from going out.
    """
    try:
        with get_session() as session:
            session.add(
                AlertFired(
                    company_id=_company_for(payload, company_id),
                    alert_id=str(alert_id)[:128],
                    entity=str(entity or "")[:255],
                    severity=(str(severity)[:32] if severity else None),
                    fired_at=utcnow(),
                    payload=payload,
                    acknowledged=False,
                    suppressed_reason=(str(suppressed_reason)[:64] if suppressed_reason else None),
                    rules_version=_rules_version()[:64],
                )
            )
    except Exception as exc:  # pragma: no cover - DB unavailable
        logger.error("could not record notification to alerts_fired: %s", exc)


# ==========================================================================
# Delivery
# ==========================================================================


def _max_backoff() -> float:
    return float(_as_number(os.environ.get("NOTIFY_MAX_BACKOFF_SECONDS")) or 300.0)


def _deliver_with_retries(
    channel: Channel,
    recipient: str,
    message: RenderedMessage,
    context: dict,
    delivery: dict,
) -> dict:
    """Send, retrying transport failures with the configured backoff."""
    max_retries = _as_int(delivery.get("max_retries"))
    max_retries = 3 if max_retries is None else max(0, max_retries)
    backoff = delivery.get("retry_backoff_seconds") or [5, 30, 120]
    if not isinstance(backoff, (list, tuple)) or not backoff:
        backoff = [5, 30, 120]

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return channel.send(recipient=recipient, message=message, context=context)
        except (PermanentSendError, MissingCredentials):
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= max_retries:
                break
            delay = min(
                float(_as_number(backoff[min(attempt, len(backoff) - 1)]) or 5.0),
                _max_backoff(),
            )
            logger.warning(
                "%s send attempt %d/%d failed (%s); retrying in %.0fs",
                channel.name,
                attempt + 1,
                max_retries + 1,
                exc,
                delay,
            )
            _sleep(delay)
    raise TransientSendError(
        f"{channel.name} send failed after {max_retries + 1} attempts: {last_error}"
    )


def _escalate(reason: str, context: dict) -> None:
    """A notifier that fails silently is the failure mode this system exists
    to prevent, so a dead channel raises a pipeline_failure of its own.

    Guarded against re-entry: if the escalation itself cannot be delivered we
    log CRITICAL rather than emitting another pipeline_failure forever.
    """
    if getattr(_local, "escalating", False):
        logger.critical("NOTIFIER ESCALATION FAILED TO DELIVER: %s", reason)
        return
    _local.escalating = True
    try:
        from ..core.events import emit_pipeline_failure

        emit_pipeline_failure(
            "notifier",
            reason,
            report_type=context.get("report_type"),
            event_type=context.get("event_type"),
        )
    except Exception as exc:  # pragma: no cover
        logger.critical("could not escalate notifier failure (%s): %s", reason, exc)
    finally:
        _local.escalating = False


def _send_one(
    *,
    route: dict,
    role: str,
    message: RenderedMessage,
    context: dict,
    notifications: dict,
    dry_run: bool,
    record_alert_id: str,
    record_entity: str,
    severity: str | None,
) -> bool:
    """Render-and-send for one (route, recipient) pair. Returns True on delivery."""
    channel_name = str(route.get("channel") or "sms")
    channel = get_channel(channel_name)
    if channel is None:
        _record(
            alert_id=record_alert_id,
            entity=record_entity,
            severity=severity,
            payload={"channel": channel_name, "role": role, "error": "unknown_channel"},
            suppressed_reason="unknown_channel",
        )
        return False

    # A webhook route may name its own URL env var; otherwise fall back to the
    # recipients mapping like every other channel.
    recipient: str | None
    if route.get("url_env"):
        recipient = (os.environ.get(str(route["url_env"])) or "").strip() or None
        if recipient is None:
            logger.warning("route url_env %s is unset", route["url_env"])
    else:
        recipient = resolve_recipient(role, channel_name, notifications)

    record_payload: dict[str, Any] = {
        "channel": channel_name,
        "role": role,
        "recipient": _mask(recipient, channel_name),
        "template": message.template_name,
        "subject": message.subject,
        "body": message.body,
        "report_type": context.get("report_type"),
        "event_type": context.get("event_type"),
        "dry_run": bool(dry_run),
    }

    if recipient is None:
        record_payload["error"] = "unresolved_recipient"
        _record(
            alert_id=record_alert_id,
            entity=record_entity,
            severity=severity,
            payload=record_payload,
            suppressed_reason="no_recipient",
        )
        logger.error(
            "no %s address for role %r; message NOT delivered:\n%s",
            channel_name,
            role,
            message.body,
        )
        return False

    if dry_run:
        logger.info(
            "[DRY RUN] %s -> %s (%s) template=%s subject=%r\n%s",
            channel_name,
            role,
            _mask(recipient, channel_name),
            message.template_name,
            message.subject,
            message.body,
        )
        _record(
            alert_id=record_alert_id,
            entity=record_entity,
            severity=severity,
            payload=record_payload,
            suppressed_reason=None,
        )
        return True

    available, reason = channel.available()
    if not available:
        # Fall back to logging rather than crashing the pipeline: the numbers
        # were still computed and the operator can still read them.
        logger.warning(
            "%s channel unavailable (%s); logging the message instead of sending:\n%s",
            channel_name,
            reason,
            message.body,
        )
        record_payload["error"] = f"channel_unavailable: {reason}"
        _record(
            alert_id=record_alert_id,
            entity=record_entity,
            severity=severity,
            payload=record_payload,
            suppressed_reason="channel_unavailable",
        )
        return False

    delivery = notifications.get("delivery") or {}
    try:
        result = _deliver_with_retries(channel, recipient, message, context, delivery)
    except Exception as exc:
        logger.error("%s delivery to %r failed: %s", channel_name, role, exc)
        logger.error("undelivered message body:\n%s", message.body)
        record_payload["error"] = f"{type(exc).__name__}: {exc}"
        _record(
            alert_id=record_alert_id,
            entity=record_entity,
            severity=severity,
            payload=record_payload,
            suppressed_reason="send_failed",
        )
        if (notifications.get("delivery") or {}).get("escalate_failed_send", True):
            _escalate(f"{channel_name} delivery to {role} failed: {exc}", context)
        return False

    record_payload["result"] = result
    _record(
        alert_id=record_alert_id,
        entity=record_entity,
        severity=severity,
        payload=record_payload,
        suppressed_reason=None,
    )
    logger.info(
        "%s delivered to %s (%s) via template %s",
        channel_name,
        role,
        _mask(recipient, channel_name),
        message.template_name,
    )
    return True


#: Values that turn a route off in notifications.yaml.
_FALSEY = {"0", "false", "no", "off"}


def route_enabled(route: Mapping[str, Any]) -> bool:
    """Is this route switched on? ``enabled:``, default true.

    THE SHIPPED CONFIG HAS EVERY ROUTE DISABLED. The dashboard is the delivery
    mechanism now -- no SMS, no email -- and this flag is how that is expressed
    without deleting the routing table. The routes, the recipients, the templates
    and the quiet hours all stay in the file, correct and documented, so turning
    a channel back on is ``enabled: true`` on one line: a YAML edit, no code
    change, no redeploy (config/notifications.yaml is re-stat'd on every access).

    Deleting the routes instead would have been the same behaviour and a much
    worse artefact -- the next company to deploy this would have to reconstruct
    a working notifications.yaml from the docs rather than flip a flag.
    """
    value = route.get("enabled")
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in _FALSEY


def _route_matches(
    route: Mapping[str, Any],
    *,
    report: str | None,
    event: str | None,
    severity: str | None,
) -> bool:
    """Does this route cover this report / event, ignoring whether it is enabled?"""
    if report is not None:
        return str(route.get("report") or "") == report
    if event is None:
        return False
    if str(route.get("event") or "") != event:
        return False
    wanted = route.get("severity")
    if wanted is None:
        return True
    wanted_set = {wanted} if isinstance(wanted, str) else set(wanted)
    wanted_set = {str(item).strip().lower() for item in wanted_set}
    return str(severity or "").strip().lower() in wanted_set


def _matching_routes(notifications: dict, *, report: str | None = None, event: str | None = None,
                     severity: str | None = None) -> list[dict]:
    """Enabled routes covering this report or event, in file order."""
    return [
        route
        for route in _candidate_routes(notifications, report=report, event=event, severity=severity)
        if route_enabled(route)
    ]


def _candidate_routes(notifications: dict, *, report: str | None = None, event: str | None = None,
                      severity: str | None = None) -> list[dict]:
    """Every route covering this report or event, enabled or not.

    The distinction matters: "no route is configured for the daily report" is a
    misconfiguration worth shouting about, and "the daily route exists and is
    switched off because the board is the delivery" is the intended state. They
    must not produce the same log line, or the intended state trains everyone to
    ignore the real one.
    """
    routes = notifications.get("routes") or []
    if not isinstance(routes, list):
        logger.error("notifications.yaml routes must be a list")
        return []

    return [
        route
        for route in routes
        if isinstance(route, dict)
        and _route_matches(route, report=report, event=event, severity=severity)
    ]


def _route_template_name(route: dict, event_type: str, channel: str, notifications: dict) -> str:
    """Template for an event route. Config first, then convention."""
    explicit = route.get("template")
    if explicit:
        return str(explicit)
    templates = notifications.get("templates") or {}
    for candidate in (f"{event_type}_{channel}", f"{event_type}_sms", event_type):
        if candidate in templates or candidate in _BUILTIN_TEMPLATES:
            return candidate
    return f"{event_type}_sms"


# ==========================================================================
# Public entry points (module contract)
# ==========================================================================


def dispatch_report(
    report_type: str,
    metrics: dict,
    analysis: dict | None = None,
    *,
    dry_run: bool | None = None,
) -> None:
    """Send every route configured for ``report_type``.

    Daily deliberately matches two routes (a short SMS and a full email); all
    matching routes fire.
    """
    dry_run = dry_run_enabled() if dry_run is None else bool(dry_run)
    try:
        notifications = get_notifications()
    except Exception as exc:
        logger.critical("cannot read notifications.yaml: %s", exc)
        _escalate(f"notifications.yaml unreadable: {exc}", {"report_type": report_type})
        return

    routes = _matching_routes(notifications, report=report_type)
    if not routes:
        candidates = _candidate_routes(notifications, report=report_type)
        if candidates:
            # The intended shipped state: the routes exist and are switched off
            # because the dashboard is the delivery mechanism. Logged at INFO and
            # recorded with an honest reason, so /health can show that the report
            # was produced and deliberately not sent.
            logger.info(
                "report %s produced; all %d configured route(s) are disabled "
                "(notifications.yaml -> enabled: false). The dashboard is the delivery.",
                report_type,
                len(candidates),
            )
            _record(
                alert_id=f"report_{report_type}",
                entity="",
                severity=None,
                payload={"report_type": report_type, "routes_disabled": len(candidates)},
                suppressed_reason="delivery_disabled",
            )
            return
        # Not a crash, but not silence either: a report nobody is routed to is
        # a configuration mistake worth shouting about.
        logger.warning("no notification route matches report %r", report_type)
        _record(
            alert_id=f"report_{report_type}",
            entity="",
            severity=None,
            payload={"report_type": report_type, "error": "no_matching_route"},
            suppressed_reason="no_matching_route",
        )
        return

    context = build_context(report_type, metrics, analysis, notifications)

    for route in routes:
        template_name = str(route.get("template") or f"{report_type}_summary")
        message = render_template(template_name, context, notifications)

        suppression = evaluate_suppression(
            event_type=None,
            alert_id=None,
            severity=route.get("severity"),
            notifications=notifications,
        )
        if suppression.suppressed:
            logger.info(
                "report %s over %s suppressed (%s)",
                report_type,
                route.get("channel"),
                suppression.reason,
            )
            _record(
                alert_id=f"report_{report_type}",
                entity="",
                severity=route.get("severity"),
                payload={
                    "report_type": report_type,
                    "channel": route.get("channel"),
                    "template": template_name,
                    "body": message.body,
                },
                suppressed_reason=suppression.reason,
            )
            continue

        for role in _roles(route):
            _send_one(
                route=route,
                role=role,
                message=message,
                context=context,
                notifications=notifications,
                dry_run=dry_run,
                record_alert_id=f"report_{report_type}",
                record_entity=role,
                severity=route.get("severity"),
            )


def dispatch_event(
    event_type: str,
    payload: dict,
    *,
    dry_run: bool | None = None,
) -> None:
    """Send every route configured for ``event_type`` ("alert" or "pipeline_failure").

    Called by core.events.emit_event. Never raises: the caller is usually
    already handling a failure and must not be derailed by a second one.
    """
    dry_run = dry_run_enabled() if dry_run is None else bool(dry_run)
    payload = dict(payload or {})
    non_suppressible = is_non_suppressible(event_type)

    try:
        notifications = get_notifications()
    except Exception as exc:
        logger.critical(
            "cannot read notifications.yaml while handling %s: %s\npayload=%r",
            event_type,
            exc,
            payload,
        )
        return

    severity = str(payload.get("severity") or ("high" if non_suppressible else "medium"))
    alert_id = str(payload.get("alert_id") or event_type)
    entity = str(payload.get("entity") or payload.get("stage") or "")

    routes = _matching_routes(notifications, event=event_type, severity=severity)
    if not routes:
        candidates = _candidate_routes(notifications, event=event_type, severity=severity)
        if candidates:
            # Switched off on purpose. The row below is not bookkeeping -- it is
            # the delivery: web/queries.pipeline_banner() reads alerts_fired and
            # puts a pipeline_failure on a red banner across every tab of the
            # board, which is the channel that replaced the SMS. A failure whose
            # only trace was a log file inside a container would be invisible.
            level = logger.critical if non_suppressible else logger.info
            level(
                "%s recorded; all %d configured route(s) are disabled. It will appear on "
                "the dashboard banner, which is the delivery mechanism. payload=%r",
                event_type,
                len(candidates),
                payload,
            )
            _record(
                alert_id=alert_id,
                entity=entity,
                severity=severity,
                payload={**payload, "routes_disabled": len(candidates)},
                suppressed_reason="delivery_disabled",
            )
            return
        level = logger.critical if non_suppressible else logger.warning
        level(
            "no notification route matches event %s severity %s; payload=%r",
            event_type,
            severity,
            payload,
        )
        _record(
            alert_id=alert_id,
            entity=entity,
            severity=severity,
            payload={**payload, "error": "no_matching_route"},
            suppressed_reason="no_matching_route",
        )
        return

    context = _event_context(event_type, payload, notifications)

    suppression = evaluate_suppression(
        event_type=event_type,
        alert_id=(None if non_suppressible else alert_id),
        entity=entity,
        severity=severity,
        notifications=notifications,
        company_id=_company_for(payload),
    )
    if suppression.suppressed:
        logger.info(
            "event %s (%s/%s) suppressed: %s",
            event_type,
            alert_id,
            entity or "-",
            suppression.reason,
        )
        _record(
            alert_id=alert_id,
            entity=entity,
            severity=severity,
            payload={**payload, "suppressed": True},
            suppressed_reason=suppression.reason,
        )
        return

    for route in routes:
        channel_name = str(route.get("channel") or "sms")
        template_name = _route_template_name(route, event_type, channel_name, notifications)
        message = render_template(template_name, context, notifications)
        for role in _roles(route):
            _send_one(
                route=route,
                role=role,
                message=message,
                context=context,
                notifications=notifications,
                dry_run=dry_run,
                record_alert_id=alert_id,
                record_entity=entity or role,
                severity=severity,
            )


def _roles(route: dict) -> list[str]:
    to = route.get("to") or []
    if isinstance(to, str):
        to = [to]
    roles = [str(role) for role in to if role]
    if not roles:
        logger.error("route %r has no recipients", route)
    return roles

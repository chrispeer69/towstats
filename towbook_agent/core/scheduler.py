"""APScheduler driver and the one implementation of the pipeline.

Two things live here, and they belong together:

1. :func:`run_pipeline` -- acquire -> ingest -> classify -> metrics -> alerts ->
   analyst -> notify, for one window. The CLI and the scheduler both call it, so
   ``python -m towbook_agent run --report daily --date 2026-07-27`` and the
   06:00 cron tick execute exactly the same code path.
2. :func:`run_scheduler` -- a BlockingScheduler whose job table is built
   *entirely* from ``config/schedule.yaml``.

Hard constraint #2 (rules are data)
-----------------------------------
Nothing about the job table is hardcoded. Adding a fourth report, retiming the
hourly job, or pointing a job at a second Towbook account is an edit to
schedule.yaml -- no code change, no redeploy. The file's digest is watched and
the job set is rebuilt in place when it changes, so a long-running scheduler
picks the edit up without a restart.

Range keywords are a registry, so a new window type is one function::

    @register_range("previous_calendar_month")
    def previous_calendar_month(now): ...

Hard constraint #4 (idempotency)
--------------------------------
The window is derived from the clock at fire time, never from "when did we last
run". ``run_id`` is deterministic -- ``{report}-{account}-{window start UTC}``
-- so re-running the same window updates the same ``runs`` row, upserts the same
``requests`` rows on ``request_id``, and upserts the same metrics row on its
unique window key. Re-running yesterday's daily report produces yesterday's
numbers again, not double them.

Hard constraint #5 (silence is never success)
---------------------------------------------
* :func:`run_pipeline` never raises. Every failure is logged with a traceback,
  emitted as a ``pipeline_failure`` event, recorded on the ``runs`` row, and
  reflected in a non-zero :attr:`PipelineResult.exit_code`.
* A misconfigured schedule.yaml emits ``pipeline_failure`` rather than quietly
  scheduling nothing.
* :func:`watchdog_check` runs on its own timer and emits ``pipeline_failure``
  when a report type has had no successful run within its cron interval plus a
  grace period. That is the case a try/except cannot catch: the process died,
  the machine slept, the cron entry was deleted. No report at all has to be
  louder than a wrong one.

Dry runs
--------
``--dry-run`` sets the ``TOWBOOK_DRY_RUN`` environment variable for the duration
of the pipeline and passes ``dry_run=True`` to any agent callable whose
signature accepts it. Agents that send anything (notifier, and the alert path
through ``core.events``) must honour it. Payloads also carry ``"dry_run": True``.

Alert rows
----------
This module emits alerts; it deliberately does not write ``alerts_fired`` rows.
That table carries ``suppressed_reason``, which only the notifier can know, so
the notifier owns the row.
"""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import threading
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence
from zoneinfo import ZoneInfo

from . import companies as _companies
from .config_loader import CONFIG, ConfigError, get_schedule, rules_version
from .db import get_session, init_db
from .events import emit_pipeline_failure
from .models import DEFAULT_ACCOUNT_ID, Run, to_utc_naive, utcnow
from .paths import RAW_DIR, ensure_dirs

__all__ = [
    # timezone / clock
    "DEFAULT_TIMEZONE",
    "local_timezone",
    "timezone_name",
    "now_local",
    # range registry
    "RANGE_FUNCS",
    "register_range",
    "resolve_range",
    "previous_full_hour",
    "previous_calendar_day",
    "trailing_7_days",
    "previous_calendar_week",
    "previous_calendar_month",
    "trailing_24_hours",
    # windows
    "window_for",
    "metrics_key_for",
    "week_start_for",
    "month_start_for",
    "summarize_metrics",
    "cron_trigger",
    "translate_cron_day_of_week",
    "REPORT_TYPES",
    "DEFAULT_PAGE_SIZE",
    "default_page_size",
    # acquisition source
    "SOURCES",
    "DEFAULT_SOURCE",
    "default_source",
    # pipeline
    "PipelineResult",
    "run_pipeline",
    "make_run_id",
    "newest_archived_xlsx",
    "newest_archived_payload",
    # dry run
    "DRY_RUN_ENV",
    "dry_run_env",
    "is_dry_run",
    # schedule
    "JobSpec",
    "ScheduleError",
    "load_job_specs",
    "build_scheduler",
    "apply_schedule",
    "run_scheduler",
    "watchdog_check",
    "overdue_reports",
    # in-process scheduler (the deployed web service runs the jobs itself)
    "run_scheduler_enabled",
    "start_background_scheduler",
    "stop_background_scheduler",
    "background_scheduler_status",
]

logger = logging.getLogger(__name__)


# ==========================================================================
# Errors
# ==========================================================================


class ScheduleError(RuntimeError):
    """config/schedule.yaml is malformed or names something that does not exist."""


class PipelineError(RuntimeError):
    """A pipeline stage failed. Always converted into a pipeline_failure event."""


# ==========================================================================
# Timezone and clock
# ==========================================================================

DEFAULT_TIMEZONE: str = "America/Detroit"

_tz_cache: dict[str, Any] = {}


def timezone_name() -> str:
    """Return the configured local timezone name (TZ env var)."""
    return (os.environ.get("TZ") or "").strip() or DEFAULT_TIMEZONE


def local_timezone():
    """Return the local ``tzinfo`` used for every report window.

    Storage is UTC everywhere; local time exists only to decide what "the
    previous calendar day" means and to render times for humans.
    """
    name = timezone_name()
    cached = _tz_cache.get(name)
    if cached is not None:
        return cached
    try:
        tz = ZoneInfo(name)
    except Exception:
        logger.error(
            "TZ=%r is not a known timezone; falling back to %s", name, DEFAULT_TIMEZONE
        )
        try:
            tz = ZoneInfo(DEFAULT_TIMEZONE)
        except Exception:  # pragma: no cover - tzdata missing entirely
            logger.error("timezone database unavailable; falling back to UTC")
            tz = timezone.utc
    _tz_cache[name] = tz
    return tz


def now_local() -> datetime:
    """Current time as a timezone-aware datetime in the configured local zone."""
    return datetime.now(local_timezone())


def _floor_day(moment: datetime) -> datetime:
    """Local midnight at the start of ``moment``'s calendar day."""
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _floor_hour_utc(moment: datetime) -> datetime:
    """Top of ``moment``'s hour, expressed in UTC.

    Hour arithmetic is done in UTC on purpose. Adding or subtracting an hour to
    a timezone-aware local datetime is *wall clock* arithmetic in Python, which
    silently repeats or skips an hour at a DST boundary. Calendar-day arithmetic
    below is deliberately the opposite: there, wall clock is the correct
    behaviour, because a day is a day whether it is 23, 24 or 25 hours long.
    """
    return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


# ==========================================================================
# Range registry -- "a new range type is one function"
# ==========================================================================

Window = tuple[datetime, datetime]
RangeFunc = Callable[[datetime], Window]

#: keyword used in schedule.yaml -> callable(now_local) -> (start, end), both
#: timezone-aware in local time, half-open [start, end).
RANGE_FUNCS: dict[str, RangeFunc] = {}


def register_range(name: str) -> Callable[[RangeFunc], RangeFunc]:
    """Register a range keyword usable from ``schedule.yaml``."""

    def decorator(func: RangeFunc) -> RangeFunc:
        RANGE_FUNCS[name] = func
        return func

    return decorator


@register_range("previous_full_hour")
def previous_full_hour(now: datetime) -> Window:
    """The clock hour that just closed. 14:07 -> [13:00, 14:00)."""
    tz = now.tzinfo or local_timezone()
    end_utc = _floor_hour_utc(now)
    start_utc = end_utc - timedelta(hours=1)
    return start_utc.astimezone(tz), end_utc.astimezone(tz)


@register_range("previous_calendar_day")
def previous_calendar_day(now: datetime) -> Window:
    """Yesterday, local midnight to local midnight."""
    end = _floor_day(now)
    return end - timedelta(days=1), end


@register_range("trailing_7_days")
def trailing_7_days(now: datetime) -> Window:
    """The seven whole days ending at last local midnight."""
    end = _floor_day(now)
    return end - timedelta(days=7), end


@register_range("previous_calendar_week")
def previous_calendar_week(now: datetime) -> Window:
    """Last Monday 00:00 to this Monday 00:00, local.

    Not used by the shipped schedule; available for a job that must always
    align to calendar weeks regardless of which day it runs on.
    """
    today = _floor_day(now)
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(days=7), this_monday


@register_range("previous_calendar_month")
def previous_calendar_month(now: datetime) -> Window:
    """The month that just closed: 1st 00:00 local to the next 1st 00:00 local.

    Wall-clock arithmetic on purpose, exactly like ``previous_calendar_day``: a
    month is a month whether or not a DST transition fell inside it. Day 1 is
    reached by flooring the day and replacing ``day``, never by subtracting 30
    days -- February and August do not have the same length and the unique key
    on ``metrics_monthly.month_start`` has to land on the 1st either way.
    """
    first_of_this_month = _floor_day(now).replace(day=1)
    # One day back from the 1st is the last day of the previous month; flooring
    # that to its own 1st is the previous month's start, with no month table.
    first_of_previous = (first_of_this_month - timedelta(days=1)).replace(day=1)
    return first_of_previous, first_of_this_month


@register_range("trailing_24_hours")
def trailing_24_hours(now: datetime) -> Window:
    """The 24 whole hours ending at the top of the current hour."""
    tz = now.tzinfo or local_timezone()
    end_utc = _floor_hour_utc(now)
    return (end_utc - timedelta(hours=24)).astimezone(tz), end_utc.astimezone(tz)


def resolve_range(name: str, now: datetime | None = None) -> Window:
    """Resolve a range keyword into a half-open local window ``[start, end)``."""
    try:
        func = RANGE_FUNCS[name]
    except KeyError:
        raise ScheduleError(
            f"unknown range {name!r} in config/schedule.yaml; "
            f"known ranges: {', '.join(sorted(RANGE_FUNCS))}"
        ) from None
    moment = now or now_local()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=local_timezone())
    return func(moment)


# ==========================================================================
# Report types and windows
# ==========================================================================

#: The report types the metrics agent implements. A schedule.yaml job whose
#: name is not one of these must carry an explicit ``report:`` key.
REPORT_TYPES: tuple[str, ...] = ("hourly", "daily", "weekly", "monthly")

#: Fallback page sizes, used only when schedule.yaml does not name one.
DEFAULT_PAGE_SIZE: dict[str, int] = {
    "hourly": 50,
    "daily": 1000,
    "weekly": 1000,
    "monthly": 1000,
}

#: Default range per report type, used by ``run --report X`` with no explicit
#: window. Overridden by whatever schedule.yaml says for that report.
DEFAULT_RANGE: dict[str, str] = {
    "hourly": "previous_full_hour",
    "daily": "previous_calendar_day",
    "weekly": "trailing_7_days",
    "monthly": "previous_calendar_month",
}


def window_for(report_type: str, anchor: datetime) -> Window:
    """Build the canonical window that starts at ``anchor`` for ``report_type``.

    ``anchor`` is a local, timezone-aware datetime. It is floored to the natural
    boundary for the report (hour, or midnight) so that a sloppy ``--datetime``
    still produces a clean window.
    """
    tz = anchor.tzinfo or local_timezone()
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=tz)

    if report_type == "hourly":
        start_utc = _floor_hour_utc(anchor)
        return start_utc.astimezone(tz), (start_utc + timedelta(hours=1)).astimezone(tz)
    if report_type == "daily":
        start = _floor_day(anchor)
        return start, start + timedelta(days=1)
    if report_type == "weekly":
        start = _floor_day(anchor)
        return start, start + timedelta(days=7)
    if report_type == "monthly":
        # Floored to the 1st, and the end is the NEXT 1st -- computed by adding
        # 32 days and flooring again, which lands in the following month from
        # any day of any month without a length table.
        start = _floor_day(anchor).replace(day=1)
        return start, (start + timedelta(days=32)).replace(day=1)
    raise ScheduleError(
        f"unknown report type {report_type!r}; expected one of {', '.join(REPORT_TYPES)}"
    )


def week_start_for(value: _date) -> _date:
    """The Monday on or before ``value``.

    Matches ``agents.metrics.week_start_for``. Duplicated rather than imported
    so that ``core`` keeps no dependency on ``agents``; the two must agree,
    because ``metrics_weekly.week_start`` is a unique key and a disagreement
    would write the same week twice under two different dates.
    """
    return value - timedelta(days=value.weekday())


def month_start_for(value: _date) -> _date:
    """The 1st of the calendar month containing ``value``.

    Matches ``agents.metrics.month_start_for``, and duplicated here for the same
    reason ``week_start_for`` is: ``core`` keeps no dependency on ``agents``, and
    ``metrics_monthly.month_start`` is a unique key, so the two must agree or a
    month would be written twice under two different dates.
    """
    return value.replace(day=1)


def metrics_key_for(report_type: str, window_start: datetime) -> datetime | _date:
    """Return the argument the metrics agent expects for this window.

    All three ``compute_*`` functions work in **local** time:
    ``compute_hourly`` takes the local hour start (an aware datetime is
    converted for it, which is why the aware value is handed over rather than
    a naive one -- a naive datetime would be *assumed* local, and the UTC
    equivalent would silently land four or five hours off). ``compute_daily``
    and ``compute_weekly`` take a local calendar date, matching
    ``metrics_daily.date`` and ``metrics_weekly.week_start``.
    """
    if report_type == "hourly":
        return window_start
    if report_type == "weekly":
        return week_start_for(window_start.date())
    if report_type == "monthly":
        return month_start_for(window_start.date())
    return window_start.date()


def default_page_size(report_type: str) -> int:
    """Page size for ``report_type``, taken from schedule.yaml when present."""
    try:
        for spec in load_job_specs(strict=False):
            if spec.report_type == report_type and spec.page_size:
                return spec.page_size
    except Exception:  # pragma: no cover - config problems reported elsewhere
        pass
    return DEFAULT_PAGE_SIZE.get(report_type, 1000)


# ==========================================================================
# Acquisition source
# ==========================================================================

#: Which acquisition agent feeds a run.
#:
#: ``api`` -- agents/acquisition_api.acquire_api. The JSON endpoint. PRIMARY:
#:   no browser, ~1.5 s per 1,000 rows, and it returns ``callRequestId``, a real
#:   unique id on every row. The UI export has none -- its only id-shaped column
#:   is blank on exactly the unaccepted offers the acceptance rate is measured
#:   against -- so the UI path has to synthesise a fingerprint instead.
#: ``ui``  -- agents/acquisition.acquire. Playwright + "Export to Excel". Kept
#:   as the escape hatch for the day Towbook changes or revokes the endpoint.
SOURCES: tuple[str, ...] = ("api", "ui")
DEFAULT_SOURCE: str = "api"


def default_source(report_type: str | None = None) -> str:
    """Which source to use, from schedule.yaml, defaulting to ``api``."""
    try:
        for spec in load_job_specs(strict=False):
            if report_type is None or spec.report_type == report_type:
                return spec.source
    except Exception:  # pragma: no cover - config problems reported elsewhere
        pass
    return DEFAULT_SOURCE


# ==========================================================================
# Dry run plumbing
# ==========================================================================

#: Set for the duration of a dry run. Any agent that sends anything must treat
#: this as "render the message, log it, send nothing".
#:
#: ``DRY_RUN`` is the name agents.notifier.dry_run_enabled() reads;
#: ``TOWBOOK_DRY_RUN`` is the namespaced alias. Both are set, because a bare
#: ``DRY_RUN`` in a shared environment is easy to collide with and the wrong
#: outcome here is a real SMS going out during a rehearsal.
DRY_RUN_ENV: str = "DRY_RUN"
DRY_RUN_ENV_ALIASES: tuple[str, ...] = ("DRY_RUN", "TOWBOOK_DRY_RUN")

_TRUTHY = {"1", "true", "yes", "on"}


def is_dry_run() -> bool:
    """True when the current process is inside a dry run."""
    return any(
        (os.environ.get(name) or "").strip().lower() in _TRUTHY for name in DRY_RUN_ENV_ALIASES
    )


@contextmanager
def dry_run_env(enabled: bool) -> Iterator[None]:
    """Set the dry-run environment variables while the block runs, then restore."""
    if not enabled:
        yield
        return
    previous = {name: os.environ.get(name) for name in DRY_RUN_ENV_ALIASES}
    for name in DRY_RUN_ENV_ALIASES:
        os.environ[name] = "1"
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# ==========================================================================
# Agent loading and tolerant calling
# ==========================================================================


def _load_agent(name: str):
    """Import ``towbook_agent.agents.<name>``, with a legible failure.

    Imported lazily so that ``initdb``, ``serve`` and a dry run of the schedule
    do not drag Playwright or the Anthropic SDK onto the import path.
    """
    try:
        return importlib.import_module(f"..agents.{name}", package=__package__)
    except ImportError as exc:
        raise PipelineError(
            f"agent 'towbook_agent.agents.{name}' could not be imported: {exc}"
        ) from exc


def _call(func: Callable, *args: Any, **kwargs: Any) -> Any:
    """Call ``func``, silently dropping keyword arguments it does not accept.

    Used for optional extras such as ``dry_run=``. The module contracts fix the
    positional signatures; this only lets us pass a hint to an agent that wants
    one without breaking an agent that does not.
    """
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):  # builtins / C callables
        return func(*args, **kwargs)

    params = signature.parameters
    if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        kwargs = {key: value for key, value in kwargs.items() if key in params}
    return func(*args, **kwargs)


# ==========================================================================
# Pipeline result
# ==========================================================================


@dataclass
class PipelineResult:
    """Outcome of one pipeline run. Returned by :func:`run_pipeline`.

    ``status`` mirrors ``runs.status``:

    * ``succeeded`` -- every stage did its job.
    * ``partial``   -- the numbers are correct but something optional degraded
      (typically the Analyst: no API key, or the model call failed). The report
      still went out. Exit code 0.
    * ``failed``    -- a required stage failed. A pipeline_failure event was
      emitted. Exit code 1.
    """

    report_type: str
    run_id: str
    #: Which towing company this run was for -- an id from companies.yaml.
    company_id: str
    window_start: datetime
    window_end: datetime
    status: str = "failed"
    #: which acquisition path fed this run -- "api" or "ui". Recorded because
    #: "where did this number come from" is the first question anyone asks of a
    #: surprising report, and the two paths derive request_id differently.
    source: str = DEFAULT_SOURCE
    row_count: int = 0
    xlsx_path: Path | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    analysis: dict[str, Any] | None = None
    alerts: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False
    rules_version: str = ""
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None

    @property
    def account_id(self) -> str:
        """Deprecated alias for :attr:`company_id`, for callers that predate it."""
        return self.company_id

    @property
    def ok(self) -> bool:
        return self.status in ("succeeded", "partial")

    @property
    def exit_code(self) -> int:
        """0 when the report is trustworthy, 1 when it is not.

        ``run`` returns this so cron / Task Scheduler notices a failure.
        """
        return 0 if self.ok else 1

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or utcnow()
        return max(0.0, (end - self.started_at).total_seconds())

    def summary_line(self) -> str:
        offered, accepted, rate = summarize_metrics(self.metrics)
        rate_text = "--" if rate is None else f"{round(rate * 100):d}%"
        return (
            f"{self.report_type} {self.window_start:%Y-%m-%d %H:%M} -> "
            f"{self.window_end:%Y-%m-%d %H:%M} | rows {self.row_count} | "
            f"offered {offered} accepted {accepted} ({rate_text}) | {self.status.upper()}"
        )


def summarize_metrics(metrics: dict[str, Any]) -> tuple[Any, Any, float | None]:
    """Pull (offered, accepted, rate) out of a metrics dict, tolerantly.

    The metrics agent owns the shape of its document, and the shape differs by
    report: the hourly document carries ``offered`` / ``accepted`` / ``rate`` at
    the top level (the hourly SMS needs them flat), while daily and weekly keep
    them in ``totals``. Both are read here, and "?" is reported rather than
    guessed when neither is present.
    """
    if not isinstance(metrics, dict):
        return ("?", "?", None)

    totals = metrics.get("totals")
    if not isinstance(totals, dict):
        totals = {}

    def pick(*keys: str, default: Any = None) -> Any:
        for key in keys:
            if metrics.get(key) is not None:
                return metrics[key]
        for key in keys:
            if totals.get(key) is not None:
                return totals[key]
        return default

    offered = pick("offered", "offered_window", "total_offered", default="?")
    accepted = pick("accepted", "accepted_window", "total_accepted", default="?")
    rate = pick("rate", "acceptance_rate", "acceptance_rate_window")

    if rate is None:
        percent = metrics.get("rate_pct", totals.get("rate_pct"))
        if isinstance(percent, (int, float)):
            rate = percent / 100.0
    if rate is None and isinstance(offered, (int, float)) and offered:
        if isinstance(accepted, (int, float)):
            rate = accepted / offered

    try:
        rate = float(rate) if rate is not None else None
    except (TypeError, ValueError):
        rate = None
    return (offered, accepted, rate)


# ==========================================================================
# Run bookkeeping
# ==========================================================================


def make_run_id(report_type: str, company_id: str, window_start: datetime) -> str:
    """Deterministic run id -- the same window always produces the same id.

    That is what makes a re-run an update rather than a duplicate: the ``runs``
    row is upserted, and every ``requests`` row keeps pointing at one run.

    The company is IN the id, so two tenants running the same window at the
    same minute produce two runs rather than fighting over one row.
    """
    stamp = to_utc_naive(window_start)
    assert stamp is not None  # window_start is never None here
    return f"{report_type}-{company_id}-{stamp:%Y%m%dT%H%M%S}"


_db_ready = False
_db_lock = threading.Lock()


def _ensure_db() -> None:
    """Create directories and tables once per process."""
    global _db_ready
    if _db_ready:
        return
    with _db_lock:
        if _db_ready:
            return
        ensure_dirs()
        init_db()
        _db_ready = True


def _record_run_start(result: PipelineResult, page_size: int | None) -> None:
    with get_session() as session:
        run = session.get(Run, result.run_id)
        if run is None:
            run = Run(run_id=result.run_id)
            session.add(run)
        run.company_id = result.company_id
        run.report_type = result.report_type
        run.window_start = to_utc_naive(result.window_start)
        run.window_end = to_utc_naive(result.window_end)
        run.page_size = page_size
        run.status = "started"
        run.row_count = None
        run.rules_version = result.rules_version
        run.started_at = result.started_at
        run.finished_at = None
        run.error_message = None
        run.source_file = None


def _record_run_finish(result: PipelineResult) -> None:
    message = "; ".join(result.errors + result.warnings) or None
    if message and len(message) > 4000:
        message = message[:3997] + "..."
    try:
        with get_session() as session:
            run = session.get(Run, result.run_id)
            if run is None:  # pragma: no cover - start always writes it
                run = Run(run_id=result.run_id)
                session.add(run)
            run.status = result.status
            run.row_count = result.row_count
            run.finished_at = result.finished_at or utcnow()
            run.error_message = message
            run.source_file = str(result.xlsx_path) if result.xlsx_path else None
    except Exception:
        # Never let bookkeeping mask the real outcome.
        logger.exception("could not update runs row %s", result.run_id)


# ==========================================================================
# Source file helpers
# ==========================================================================


#: Every payload shape the acquirers archive, newest-first search order.
#: ``.json`` -- the API path. ``.csv`` -- what "Export to Excel" actually
#: delivers. ``.xlsx`` / ``.xls`` -- what it claims to, and what the fixtures use.
_ARCHIVE_SUFFIXES: tuple[str, ...] = (".json", ".csv", ".xlsx", ".xls")


def newest_archived_payload(root: Path | None = None) -> Path | None:
    """Most recently modified acquired payload under ``raw/``, or None.

    Backs ``--no-acquire``: re-run the pipeline against the last payload instead
    of hitting the portal again. Any archived shape counts -- ingestion
    dispatches on the file's own format, so a JSON pull and a CSV export are
    equally re-ingestable.

    ``*.meta.json`` sidecars are excluded: they describe a run, they are not a
    run's data, and ingesting one would abort on required-key validation.
    """
    base = root or RAW_DIR
    if not base.exists():
        return None
    candidates: list[Path] = []
    for suffix in _ARCHIVE_SUFFIXES:
        candidates.extend(
            path
            for path in base.rglob(f"*{suffix}")
            if path.is_file()
            and not path.name.startswith("~$")
            and not path.name.endswith(".meta.json")
        )
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def newest_archived_xlsx(root: Path | None = None) -> Path | None:
    """Deprecated alias for :func:`newest_archived_payload`.

    Kept because "xlsx" is the name the build document used, and because the
    CLI's ``--no-acquire`` help text has referred to it since day one.
    """
    return newest_archived_payload(root)


#: Attribute names searched, in order, for "how many rows did this ingest
#: land". ``rows_upserted`` is what agents.ingestion.IngestResult calls it; the
#: rest are tolerated aliases so a change there does not silently report 0.
_ROW_COUNT_KEYS: tuple[str, ...] = (
    "rows_upserted",
    "row_count",
    "rows_ingested",
    "rows_read",
    "rows",
    "ingested",
    "count",
    "total",
)


def _row_count_from(ingest_result: Any) -> int:
    """Extract a row count from whatever ``IngestResult`` turns out to be."""
    if ingest_result is None:
        return 0
    if isinstance(ingest_result, int):
        return ingest_result
    if isinstance(ingest_result, dict):
        for key in _ROW_COUNT_KEYS:
            value = ingest_result.get(key)
            if isinstance(value, int):
                return value
        return 0
    for attribute in _ROW_COUNT_KEYS:
        value = getattr(ingest_result, attribute, None)
        if isinstance(value, int):
            return value
    inserted = getattr(ingest_result, "rows_inserted", getattr(ingest_result, "inserted", None))
    updated = getattr(ingest_result, "rows_updated", getattr(ingest_result, "updated", None))
    if isinstance(inserted, int) or isinstance(updated, int):
        return (inserted or 0) + (updated or 0)
    try:
        return len(ingest_result)  # type: ignore[arg-type]
    except TypeError:
        return 0


# ==========================================================================
# Alerts
# ==========================================================================


def _alerts_from_metrics(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the alerts that ``agents.metrics`` already evaluated and emitted.

    Alert *evaluation* deliberately does not happen here. ``compute_hourly`` /
    ``compute_daily`` / ``compute_weekly`` build the evaluation contexts (they
    are the only component that knows whether a context is global, per client or
    per request), call ``classifier.evaluate_alerts``, apply the
    ``same_alert_same_entity`` dedupe window from notifications.yaml, emit
    through ``core.events``, and put what fired in ``document["alerts"]``.
    Re-evaluating them here would send every alert twice.
    """
    if not isinstance(metrics, dict):
        return []
    alerts = metrics.get("alerts")
    if not isinstance(alerts, list):
        return []
    return [alert for alert in alerts if isinstance(alert, dict)]


# ==========================================================================
# The pipeline
# ==========================================================================


def run_pipeline(
    report_type: str,
    window_start: datetime,
    window_end: datetime,
    *,
    page_size: int | None = None,
    company_id: str | None = None,
    account_id: str = DEFAULT_ACCOUNT_ID,
    dry_run: bool = False,
    acquire: bool = True,
    xlsx_path: Path | None = None,
    skip_ingest: bool = False,
    notify: bool = True,
    source: str | None = None,
) -> PipelineResult:
    """Run acquire -> ingest -> classify -> metrics -> alerts -> analyst -> notify.

    Parameters
    ----------
    report_type   one of hourly / daily / weekly / monthly.
    window_start  local, timezone-aware, inclusive.
    window_end    local, timezone-aware, exclusive.
    page_size     rows per portal page; defaults to schedule.yaml's value.
                  Ignored on the ``api`` source, which caps its own page size at
                  1000 (2000 returns HTTP 500 after a 30 s server timeout).
    company_id    which towing company from config/companies.yaml this run is
                  for. Defaults to the roster's default company, which on a
                  single-company install is "default" -- the id every existing
                  row already carries. ``account_id`` is the old name and still
                  works.
    dry_run       render and log messages, send nothing.
    acquire       False re-uses an archived payload instead of the portal.
    xlsx_path     a specific payload to ingest (implies acquire=False). Any
                  archived shape: .json from the API, .csv / .xlsx from the UI.
    skip_ingest   recompute from data already in the database.
    notify        False computes and stores but sends nothing at all.
    source        "api" (default) or "ui"; see :data:`SOURCES`. None takes
                  schedule.yaml's value for this report type.

    **Never raises.** Failures are logged with a traceback, emitted as
    ``pipeline_failure``, recorded on the ``runs`` row, and reported through
    :attr:`PipelineResult.exit_code`.
    """
    company_id = _companies.resolve_company_id(
        company_id if company_id is not None else account_id
    )
    # Everything below -- the window snap, the run id, the acquirer, the
    # metrics pass -- happens in this company's timezone and against this
    # company's rules. The scope is entered here rather than deeper down so
    # that a single tenant is active for the whole run, including the parts
    # that read the clock before touching the database.
    with _companies.use_company(company_id):
        return _run_pipeline(
            report_type,
            window_start,
            window_end,
            page_size=page_size,
            company_id=company_id,
            dry_run=dry_run,
            acquire=acquire,
            xlsx_path=xlsx_path,
            skip_ingest=skip_ingest,
            notify=notify,
            source=source,
        )


def _run_pipeline(
    report_type: str,
    window_start: datetime,
    window_end: datetime,
    *,
    page_size: int | None,
    company_id: str,
    dry_run: bool,
    acquire: bool,
    xlsx_path: Path | None,
    skip_ingest: bool,
    notify: bool,
    source: str | None,
) -> PipelineResult:
    if window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=local_timezone())
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=local_timezone())

    # A week is always Monday..Sunday, because metrics_weekly.week_start is a
    # unique key and compute_weekly snaps to Monday regardless. Snapping here
    # too keeps the run_id, the runs row and the printed window describing the
    # week that was actually computed. On the shipped schedule (Mon 06:00) this
    # is a no-op; it only bites on a manual or catch-up run.
    if report_type == "weekly":
        offset = window_start.weekday()
        if offset:
            original = window_start.date()
            window_start = window_start - timedelta(days=offset)
            window_end = window_start + timedelta(days=7)
            logger.info(
                "weekly window %s snapped back to the Monday that starts its week (%s)",
                original,
                window_start.date(),
            )

    # Same argument for the month: metrics_monthly.month_start is a unique key
    # and compute_monthly snaps to the 1st regardless, so the run_id, the runs
    # row and the printed window must describe the month that was computed.
    if report_type == "monthly":
        if window_start.day != 1:
            original = window_start.date()
            window_start = window_start.replace(day=1)
            logger.info(
                "monthly window %s snapped back to the 1st of its month (%s)",
                original,
                window_start.date(),
            )
        window_end = (window_start + timedelta(days=32)).replace(day=1)

    if page_size is None:
        page_size = default_page_size(report_type)

    if source is None:
        source = default_source(report_type)
    source = str(source).strip().lower()
    if source not in SOURCES:
        logger.error(
            "unknown acquisition source %r; expected one of %s. Using %r.",
            source,
            ", ".join(SOURCES),
            DEFAULT_SOURCE,
        )
        source = DEFAULT_SOURCE

    result = PipelineResult(
        report_type=report_type,
        run_id=make_run_id(report_type, company_id, window_start),
        company_id=company_id,
        window_start=window_start,
        window_end=window_end,
        dry_run=dry_run,
        source=source,
    )

    # A dedicated flag rather than a check on result.status: "failed" is also
    # the *initial* value (so that any early return is a failure), which makes
    # the status string useless as a "did anything break" signal.
    failed = False

    def fail(stage: str, error: BaseException | str) -> None:
        """Record a fatal stage failure and alert on it."""
        nonlocal failed
        failed = True
        text = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
        result.status = "failed"
        result.errors.append(f"{stage}: {text}")
        if isinstance(error, BaseException):
            logger.error(
                "pipeline stage %s failed for %s %s\n%s",
                stage,
                report_type,
                result.run_id,
                "".join(traceback.format_exception(type(error), error, error.__traceback__)),
            )
        else:
            logger.error("pipeline stage %s failed for %s: %s", stage, result.run_id, text)
        emit_pipeline_failure(
            stage,
            error,
            report_type=report_type,
            run_id=result.run_id,
            company_id=company_id,
            account_id=company_id,
            window_start=window_start.isoformat(timespec="minutes"),
            window_end=window_end.isoformat(timespec="minutes"),
            dry_run=dry_run,
        )

    with dry_run_env(dry_run):
        try:
            result.rules_version = rules_version()
        except ConfigError as exc:
            fail("config", exc)
            result.finished_at = utcnow()
            return result

        try:
            _ensure_db()
            _record_run_start(result, page_size)
        except Exception as exc:
            fail("database", exc)
            result.finished_at = utcnow()
            return result

        logger.info(
            "pipeline start | %s | window %s -> %s (%s) | run_id=%s | company=%s | "
            "source=%s | page_size=%s | rules=%s%s",
            report_type,
            window_start.isoformat(timespec="minutes"),
            window_end.isoformat(timespec="minutes"),
            timezone_name(),
            result.run_id,
            company_id,
            source,
            page_size,
            result.rules_version,
            " | DRY RUN" if dry_run else "",
        )

        # ---- 1. acquire ---------------------------------------------------
        if not skip_ingest:
            try:
                if xlsx_path is not None:
                    result.xlsx_path = Path(xlsx_path)
                    if not result.xlsx_path.is_file():
                        raise PipelineError(f"payload not found: {result.xlsx_path}")
                    logger.info("using supplied payload %s", result.xlsx_path)
                elif acquire:
                    # Two acquirers, one contract: both return the Path of an
                    # archived payload under raw/, and ingestion dispatches on
                    # that file's own format. Nothing downstream of here knows
                    # or cares which source produced it.
                    if source == "api":
                        acquisition = _load_agent("acquisition_api")
                        produced = _call(
                            acquisition.acquire_api,
                            window_start,
                            window_end,
                            company_id=company_id,
                            dry_run=dry_run,
                        )
                    else:
                        acquisition = _load_agent("acquisition")
                        produced = _call(
                            acquisition.acquire,
                            window_start,
                            window_end,
                            page_size,
                            company_id=company_id,
                            dry_run=dry_run,
                        )
                    if not produced:
                        raise PipelineError(f"acquisition ({source}) returned no file")
                    result.xlsx_path = Path(produced)
                    if not result.xlsx_path.is_file():
                        raise PipelineError(
                            f"acquisition ({source}) reported {result.xlsx_path}, "
                            "which does not exist"
                        )
                    logger.info("acquired %s via the %s source", result.xlsx_path, source)
                else:
                    newest = newest_archived_payload()
                    if newest is None:
                        raise PipelineError(
                            "--no-acquire was requested but no archived payload exists under "
                            f"{RAW_DIR}"
                        )
                    result.xlsx_path = newest
                    logger.info("re-using newest archived payload %s", newest)
            except Exception as exc:
                fail("acquisition", exc)
                result.finished_at = utcnow()
                _record_run_finish(result)
                return result

        # ---- 2. ingest ----------------------------------------------------
        if not skip_ingest and result.xlsx_path is not None:
            try:
                ingestion = _load_agent("ingestion")
                ingest_result = _call(
                    ingestion.ingest,
                    result.xlsx_path,
                    result.run_id,
                    company_id=company_id,
                    dry_run=dry_run,
                )
                result.row_count = _row_count_from(ingest_result)
                logger.info("ingested %d row(s) from %s", result.row_count, result.xlsx_path.name)
            except Exception as exc:
                fail("ingestion", exc)
                result.finished_at = utcnow()
                _record_run_finish(result)
                return result
        elif skip_ingest:
            logger.info("ingest skipped; recomputing from stored data")

        # ---- 3. classify --------------------------------------------------
        # backfill() re-derives service_class from the immutable
        # service_type_raw, so a rules.yaml edit takes effect on the next run
        # without a migration and without touching the source strings.
        try:
            classifier = _load_agent("classifier")
            with get_session() as session:
                reclassified = classifier.backfill(session, company_id)
            logger.info("classified %s row(s) against rules %s", reclassified, result.rules_version)
        except Exception as exc:
            fail("classification", exc)
            result.finished_at = utcnow()
            _record_run_finish(result)
            return result

        # ---- 4. metrics ---------------------------------------------------
        try:
            metrics_agent = _load_agent("metrics")
            key = metrics_key_for(report_type, window_start)
            if report_type == "hourly":
                compute = metrics_agent.compute_hourly
            elif report_type == "daily":
                compute = metrics_agent.compute_daily
            elif report_type == "weekly":
                compute = metrics_agent.compute_weekly
            elif report_type == "monthly":
                compute = metrics_agent.compute_monthly
            else:
                raise PipelineError(
                    f"unknown report type {report_type!r}; expected {', '.join(REPORT_TYPES)}"
                )
            # persist=True is the default and is what makes a re-run upsert on
            # the metrics table's unique window key rather than duplicate.
            metrics = _call(compute, key, company_id=company_id)
            result.metrics = metrics if isinstance(metrics, dict) else {}
            offered, accepted, rate = summarize_metrics(result.metrics)
            logger.info(
                "metrics %s | offered=%s accepted=%s rate=%s",
                report_type,
                offered,
                accepted,
                "--" if rate is None else f"{round(rate * 100)}%",
            )
        except Exception as exc:
            fail("metrics", exc)
            result.finished_at = utcnow()
            _record_run_finish(result)
            return result

        # ---- 5. alerts ----------------------------------------------------
        # Already evaluated, deduped and emitted inside the metrics stage; this
        # only surfaces them on the result for the CLI and the run log.
        result.alerts = _alerts_from_metrics(result.metrics)
        if result.alerts:
            logger.warning(
                "%d alert(s) fired: %s",
                len(result.alerts),
                ", ".join(
                    sorted({str(alert.get("alert_id", "?")) for alert in result.alerts})
                ),
            )

        # ---- 6. analyst (the only LLM component) --------------------------
        # Degradation, not failure: the deterministic report is the deliverable
        # and it goes out with or without commentary.
        try:
            analyst = _load_agent("analyst")
            analysis = _call(
                analyst.analyze,
                result.metrics,
                report_type,
                # A rehearsal must not append speculative rule changes to
                # config/rules.proposed.yaml.
                persist_proposals=not dry_run,
                dry_run=dry_run,
            )
            result.analysis = analysis if isinstance(analysis, dict) else None
            if result.analysis:
                logger.info("analyst produced %d field(s)", len(result.analysis))
        except Exception as exc:
            message = f"analyst unavailable: {type(exc).__name__}: {exc}"
            result.warnings.append(message)
            logger.error(message, exc_info=True)
            result.analysis = None

        # ---- 7. notify ----------------------------------------------------
        if notify:
            try:
                notifier = _load_agent("notifier")
                _call(
                    notifier.dispatch_report,
                    report_type,
                    result.metrics,
                    result.analysis,
                    dry_run=dry_run,
                )
                logger.info(
                    "report %s dispatched%s", report_type, " (dry run)" if dry_run else ""
                )
            except Exception as exc:
                fail("notification", exc)
        else:
            logger.info("notification suppressed by caller")

        # ---- done ---------------------------------------------------------
        if failed:
            result.status = "failed"
        else:
            result.status = "partial" if result.warnings else "succeeded"
        result.finished_at = utcnow()
        _record_run_finish(result)

        logger.info(
            "pipeline %s in %.1fs | %s",
            result.status,
            result.duration_seconds,
            result.summary_line(),
        )
        return result


# ==========================================================================
# schedule.yaml -> job specs
# ==========================================================================


@dataclass(frozen=True)
class JobSpec:
    """One row of the ``jobs:`` list in config/schedule.yaml."""

    name: str
    cron: str
    range_name: str
    report_type: str
    page_size: int | None = None
    #: "api" (JSON endpoint, the primary source) or "ui" (Playwright export).
    source: str = DEFAULT_SOURCE
    #: Which company this job runs for. **None means EVERY enabled company in
    #: config/companies.yaml**, which is the point: one `daily` entry in
    #: schedule.yaml serves however many tenants the install has, and adding a
    #: company is an edit to companies.yaml with no change to the schedule.
    #: Naming one company here pins the job to it -- useful when a tenant needs
    #: its own cron because it is in a different timezone.
    company_id: str | None = None
    enabled: bool = True
    coalesce: bool = True
    max_instances: int = 1
    misfire_grace_time: int = 900
    grace_minutes: float = 30.0
    notify: bool = True


#: A late run of a calendar-window job still computes the *same* window, so it
#: is worth running hours late. A late run of an hour-window job would compute a
#: different hour, so its grace is short and the watchdog covers the gap.
_MISFIRE_GRACE_BY_RANGE: dict[str, int] = {
    "previous_full_hour": 900,
    "trailing_24_hours": 900,
    "previous_calendar_day": 6 * 3600,
    "trailing_7_days": 6 * 3600,
    "previous_calendar_week": 6 * 3600,
    "previous_calendar_month": 24 * 3600,
}
_DEFAULT_MISFIRE_GRACE: int = 900

_FALLBACK_INTERVAL: dict[str, timedelta] = {
    "hourly": timedelta(hours=1),
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
    # The watchdog's question is "is this report still being produced". 31 days
    # is the longest a healthy monthly job can legitimately go quiet.
    "monthly": timedelta(days=31),
}


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def _as_int(value: Any, default: int | None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _job_company(entry: dict[str, Any], defaults: dict[str, Any]) -> str | None:
    """Which company a schedule.yaml entry pins itself to, or None for all.

    ``company_id:`` is the current key; ``account_id:`` is the one the
    single-company build documented and is still honoured. The literal
    ``"default"`` that the old key defaulted to is treated as "not pinned", so
    an existing schedule.yaml written before companies existed runs every
    company rather than only the one called ``default``.
    """
    for key in ("company_id", "account_id"):
        raw = entry.get(key, defaults.get(key))
        text = str(raw or "").strip()
        if text and text != DEFAULT_ACCOUNT_ID:
            return text
    return None


def load_job_specs(strict: bool = True) -> list[JobSpec]:
    """Parse ``config/schedule.yaml`` into JobSpec objects.

    Every optional key has a code default, so the shipped file -- which carries
    only ``name`` / ``cron`` / ``range`` / ``page_size`` -- is complete. Optional
    per-job keys an operator may add without any code change:

    ``report`` (which metrics function to run when the job name is not one of
    hourly/daily/weekly), ``source`` (``api`` or ``ui``), ``company_id``
    (pin the job to ONE company in config/companies.yaml -- omit it and the job
    runs for every enabled company), ``enabled``, ``coalesce``,
    ``max_instances``, ``misfire_grace_time``, ``grace_minutes`` (watchdog),
    ``notify``.

    A top-level ``defaults:`` mapping supplies fallbacks for all of those.

    ``strict=False`` skips malformed entries instead of raising, which is what
    the scheduler wants: one bad job must not take the good ones down with it.
    Skipped jobs are logged at ERROR and emit ``pipeline_failure``.
    """
    document = get_schedule() or {}
    raw_jobs = document.get("jobs")
    if raw_jobs is None:
        raw_jobs = []
    if not isinstance(raw_jobs, list):
        raise ScheduleError("config/schedule.yaml: 'jobs' must be a list")

    defaults = document.get("defaults") or {}
    if not isinstance(defaults, dict):
        defaults = {}

    specs: list[JobSpec] = []
    for index, entry in enumerate(raw_jobs):
        try:
            if not isinstance(entry, dict):
                raise ScheduleError(f"job #{index + 1} must be a mapping, got {type(entry).__name__}")

            name = str(entry.get("name") or "").strip()
            if not name:
                raise ScheduleError(f"job #{index + 1} has no 'name'")

            cron = str(entry.get("cron") or "").strip()
            if not cron:
                raise ScheduleError(f"job {name!r} has no 'cron'")
            # Validate here as well as at add_job time, so that `schedule`'s
            # pre-flight listing and any config check catch a typo before the
            # scheduler is built.
            cron_fields = cron.split()
            if len(cron_fields) != 5:
                raise ScheduleError(
                    f"job {name!r} has cron {cron!r}; expected 5 fields "
                    "(minute hour day-of-month month day-of-week)"
                )
            translate_cron_day_of_week(cron_fields[4])

            range_name = str(entry.get("range") or "").strip()
            if not range_name:
                raise ScheduleError(f"job {name!r} has no 'range'")
            if range_name not in RANGE_FUNCS:
                raise ScheduleError(
                    f"job {name!r} uses unknown range {range_name!r}; "
                    f"known ranges: {', '.join(sorted(RANGE_FUNCS))}"
                )

            report_type = str(entry.get("report") or defaults.get("report") or name).strip()
            if report_type not in REPORT_TYPES:
                raise ScheduleError(
                    f"job {name!r} maps to report type {report_type!r}, which the metrics "
                    f"agent does not implement. Add a 'report:' key naming one of "
                    f"{', '.join(REPORT_TYPES)}."
                )

            source = str(
                entry.get("source") or defaults.get("source") or DEFAULT_SOURCE
            ).strip().lower()
            if source not in SOURCES:
                raise ScheduleError(
                    f"job {name!r} uses unknown source {source!r}; "
                    f"expected one of {', '.join(SOURCES)} "
                    "('api' = the JSON endpoint, 'ui' = the Playwright export)"
                )

            misfire_default = _MISFIRE_GRACE_BY_RANGE.get(range_name, _DEFAULT_MISFIRE_GRACE)
            misfire = _as_int(
                entry.get("misfire_grace_time", defaults.get("misfire_grace_time")),
                misfire_default,
            )

            grace_raw = entry.get("grace_minutes", defaults.get("grace_minutes"))
            try:
                grace_minutes = float(grace_raw) if grace_raw is not None else 30.0
            except (TypeError, ValueError):
                grace_minutes = 30.0

            specs.append(
                JobSpec(
                    name=name,
                    cron=cron,
                    range_name=range_name,
                    report_type=report_type,
                    page_size=_as_int(entry.get("page_size", defaults.get("page_size")), None),
                    source=source,
                    company_id=_job_company(entry, defaults),
                    enabled=_as_bool(entry.get("enabled", defaults.get("enabled")), True),
                    coalesce=_as_bool(entry.get("coalesce", defaults.get("coalesce")), True),
                    max_instances=_as_int(
                        entry.get("max_instances", defaults.get("max_instances")), 1
                    )
                    or 1,
                    misfire_grace_time=misfire or _DEFAULT_MISFIRE_GRACE,
                    grace_minutes=grace_minutes,
                    notify=_as_bool(entry.get("notify", defaults.get("notify")), True),
                )
            )
        except ScheduleError as exc:
            if strict:
                raise
            logger.error("config/schedule.yaml: %s -- job skipped", exc)
            emit_pipeline_failure("schedule", exc, config="config/schedule.yaml")

    if not specs and raw_jobs:
        message = "config/schedule.yaml defines jobs but none of them are usable"
        if strict:
            raise ScheduleError(message)
        logger.error(message)
    return specs


# ==========================================================================
# Cron
#
# schedule.yaml documents its `cron:` values as "standard 5-field cron", and
# that is what an operator will write. APScheduler's CronTrigger.from_crontab()
# does NOT implement standard cron for the day-of-week field: it numbers days
# 0=Monday..6=Sunday, while cron (and crontab(5), and every cron entry anyone
# has ever copied) numbers them 0=Sunday..6=Saturday, with 7 also Sunday.
#
# Passed straight through, the shipped weekly job "0 6 * * 1" -- Monday 06:00,
# which is what the weekly report needs, because its window is Monday..Sunday
# -- fires on TUESDAY. Every field is translated into APScheduler's own
# vocabulary below instead, using day *names*, which are unambiguous in both.
# ==========================================================================

_CRON_DOW_TO_NAME: dict[int, str] = {
    0: "sun",
    1: "mon",
    2: "tue",
    3: "wed",
    4: "thu",
    5: "fri",
    6: "sat",
}
_NAME_TO_CRON_DOW: dict[str, int] = {name: number for number, name in _CRON_DOW_TO_NAME.items()}
#: APScheduler's own ordering, used for the output so the trigger reads naturally.
_APS_DOW_ORDER: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _cron_dow_value(token: str) -> int:
    """One day-of-week token -> a standard cron day number (0=Sunday)."""
    text = token.strip().lower()
    if text.isdigit():
        number = int(text)
        if 0 <= number <= 7:
            return number % 7  # cron allows 7 for Sunday as well as 0
        raise ScheduleError(f"day-of-week {token!r} is out of range 0-7")
    if text[:3] in _NAME_TO_CRON_DOW:
        return _NAME_TO_CRON_DOW[text[:3]]
    raise ScheduleError(f"unrecognised day-of-week {token!r}")


def translate_cron_day_of_week(field: str) -> str:
    """Translate a standard-cron day-of-week field to APScheduler day names.

    Handles ``*``, single values, lists, ranges (including ones that wrap, such
    as ``fri-mon``), and steps. Every numeric form is expanded into an explicit
    list of names rather than renumbered, because ``*/2`` selects a different
    set of days under the two numbering schemes and expansion cannot get that
    subtly wrong.
    """
    text = (field or "").strip()
    if text in ("", "*", "?"):
        return "*"

    days: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue

        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            try:
                step = int(step_text)
            except ValueError:
                raise ScheduleError(f"invalid step in day-of-week {field!r}") from None
            if step < 1:
                raise ScheduleError(f"invalid step in day-of-week {field!r}")
            part = part.strip() or "*"

        if part == "*":
            sequence: list[int] = list(range(0, 7))
        elif "-" in part:
            low_text, _, high_text = part.partition("-")
            low, high = _cron_dow_value(low_text), _cron_dow_value(high_text)
            if low <= high:
                sequence = list(range(low, high + 1))
            else:  # a wrapping range such as fri-mon
                sequence = list(range(low, 7)) + list(range(0, high + 1))
        else:
            sequence = [_cron_dow_value(part)]

        days.update(value for index, value in enumerate(sequence) if index % step == 0)

    if len(days) >= 7:
        return "*"
    selected = [name for name in _APS_DOW_ORDER if _NAME_TO_CRON_DOW[name] in days]
    if not selected:
        raise ScheduleError(f"day-of-week {field!r} selects no days")
    return ",".join(selected)


def cron_trigger(cron_expr: str, tz):
    """Build a CronTrigger from a standard 5-field cron expression.

    Note: when both day-of-month and day-of-week are restricted, cron ORs them
    while APScheduler ANDs them. Not emulated -- a single CronTrigger cannot
    express the OR -- and it does not arise for any job that leaves
    day-of-month as ``*``, which every shipped job does.
    """
    from apscheduler.triggers.cron import CronTrigger

    fields = (cron_expr or "").split()
    if len(fields) != 5:
        raise ScheduleError(
            f"cron {cron_expr!r} must have exactly 5 fields "
            "(minute hour day-of-month month day-of-week)"
        )
    minute, hour, day, month, day_of_week = fields
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=translate_cron_day_of_week(day_of_week),
        timezone=tz,
    )


def _watchdog_settings() -> dict[str, Any]:
    """Watchdog knobs from schedule.yaml ``watchdog:``, with code defaults."""
    block = (get_schedule() or {}).get("watchdog") or {}
    if not isinstance(block, dict):
        block = {}
    return {
        "enabled": _as_bool(block.get("enabled"), True),
        "check_every_minutes": float(_as_int(block.get("check_every_minutes"), 15) or 15),
        "grace_minutes": float(_as_int(block.get("grace_minutes"), 30) or 30),
    }


def _config_watch_seconds() -> int:
    value = _as_int((get_schedule() or {}).get("config_watch_seconds"), 30) or 30
    return max(5, value)


# ==========================================================================
# Watchdog -- "no report at all" must be louder than "a wrong report"
# ==========================================================================

_watchdog_last_alert: dict[str, datetime] = {}
_process_start: datetime = utcnow()


def _cron_interval(cron_expr: str, tz, now: datetime, report_type: str) -> timedelta:
    """Nominal interval between two consecutive fires of ``cron_expr``."""
    try:
        trigger = cron_trigger(cron_expr, tz)
        first = trigger.get_next_fire_time(None, now)
        if first is None:
            raise ValueError("cron never fires")
        second = trigger.get_next_fire_time(first, first)
        if second is None:
            raise ValueError("cron fires only once")
        interval = second - first
        if interval > timedelta(0):
            return interval
    except Exception as exc:
        logger.debug("could not derive interval from cron %r: %s", cron_expr, exc)
    return _FALLBACK_INTERVAL.get(report_type, timedelta(days=1))


def _last_success(report_type: str, company_id: str | None = None) -> datetime | None:
    """Naive-UTC timestamp of the most recent successful run of a report type.

    ``partial`` counts as a success here: the numbers were correct and the
    report went out, only something optional degraded. The watchdog's question
    is "is this report still being produced at all", and a partial run answers
    yes. The degradation itself is already visible in ``runs.error_message``.

    Scoped to one company when asked. Unscoped -- which is what the roster-wide
    watchdog wants -- one healthy tenant would answer for a silent one.
    """
    from sqlalchemy import select

    with get_session(commit=False) as session:
        statement = (
            select(Run)
            .where(Run.report_type == report_type, Run.status.in_(("succeeded", "partial")))
            .order_by(Run.started_at.desc())
            .limit(1)
        )
        if company_id:
            statement = statement.where(Run.company_id == company_id)
        row = session.execute(statement).scalar_one_or_none()
    if row is None:
        return None
    return row.finished_at or row.started_at


def overdue_reports(
    specs: Sequence[JobSpec] | None = None,
    now: datetime | None = None,
    baseline: datetime | None = None,
    company_id: str | None = None,
) -> list[dict[str, Any]]:
    """Which report types have gone quiet, as a READ-ONLY question.

    Split out of :func:`watchdog_check` so the dashboard can ask it. The
    watchdog's job is to *shout* -- it emits a ``pipeline_failure`` and keeps a
    cooldown so it does not shout every fifteen minutes. The board's job is to
    *show*, on every page load, with no cooldown and no side effect. Sharing one
    function would have meant either the banner silencing the alert or the alert
    firing once per page view.

    ``company_id`` SCOPES THE QUESTION TO ONE TENANT, and the board always
    passes it. Without it the newest successful run of *any* company answers for
    all of them: a roster where one company is healthy and another has not run
    in a week reports both as fine, on the silent company's own Health page and
    in its own banner. That is worse than no alarm, because it is an alarm
    actively saying the opposite of the truth. Left unscoped it keeps the
    roster-wide meaning the process-level watchdog wants.

    Returns one entry per stale report type, each with ``report_type``,
    ``last_success`` (naive UTC or None), ``overdue_seconds``, ``interval_seconds``
    and a human ``message``. Empty means healthy. Never raises.
    """
    findings: list[dict[str, Any]] = []
    try:
        if specs is None:
            specs = load_job_specs(strict=False)
        if not specs:
            return findings

        tz = local_timezone()
        moment = now or datetime.now(tz)
        moment_utc = to_utc_naive(moment) or utcnow()
        settings = _watchdog_settings()
        base = baseline or _process_start

        # Several jobs can feed one report type; the tightest interval wins.
        by_report: dict[str, tuple[timedelta, float]] = {}
        for spec in specs:
            if not spec.enabled:
                continue
            interval = _cron_interval(spec.cron, tz, moment, spec.report_type)
            grace = timedelta(minutes=spec.grace_minutes or settings["grace_minutes"])
            existing = by_report.get(spec.report_type)
            candidate = (interval, grace.total_seconds())
            if existing is None or candidate[0] < existing[0]:
                by_report[spec.report_type] = candidate

        for report_type, (interval, grace_seconds) in sorted(by_report.items()):
            deadline = interval + timedelta(seconds=grace_seconds)
            last = _last_success(report_type, company_id=company_id)
            reference = last or base
            age = moment_utc - reference
            if age <= deadline:
                continue

            if last is None:
                detail = (
                    f"no successful {report_type} run has ever been recorded; "
                    f"the scheduler has been up for {age}"
                )
            else:
                detail = (
                    f"last successful {report_type} run finished "
                    f"{last.isoformat(timespec='seconds')}Z, {age} ago"
                )
            findings.append(
                {
                    "report_type": report_type,
                    "last_success": last,
                    "overdue_seconds": int(age.total_seconds()),
                    "interval_seconds": int(interval.total_seconds()),
                    "grace_seconds": int(grace_seconds),
                    "deadline_seconds": int(deadline.total_seconds()),
                    "message": (
                        f"WATCHDOG: {report_type} report is overdue. Expected every "
                        f"{interval} (+{int(grace_seconds // 60)}m grace). {detail}."
                    ),
                }
            )
    except Exception:
        # Asking the question must never be able to break the caller -- the
        # dashboard calls this on every page render.
        logger.exception("overdue report check failed")
    return findings


def watchdog_check(
    specs: Sequence[JobSpec] | None = None,
    now: datetime | None = None,
    baseline: datetime | None = None,
) -> list[dict[str, Any]]:
    """Emit ``pipeline_failure`` for any report type that has gone quiet.

    A try/except around a job cannot catch the failures that matter most: the
    process was killed, the laptop slept through the 06:00 tick, someone
    commented the job out. This does.

    For each configured report type: expected interval (derived from its cron)
    plus a grace period. If the newest successful run is older than that, the
    watchdog fires. When nothing has ever succeeded, the process start time is
    the baseline, so a freshly started scheduler does not alarm immediately.

    Returns the list of stale report descriptions it alerted on (empty when
    healthy, or when every stale report is still inside its alert cooldown).
    Never raises.

    The "which reports are stale" question itself lives in :func:`overdue_reports`
    so the dashboard can ask it without emitting anything; everything below is
    the *alerting* half -- the cooldown and the ``pipeline_failure``.
    """
    alerted: list[dict[str, Any]] = []
    try:
        settings = _watchdog_settings()
        moment_utc = to_utc_naive(now or datetime.now(local_timezone())) or utcnow()

        for finding in overdue_reports(specs=specs, now=now, baseline=baseline):
            report_type = str(finding["report_type"])
            cooldown = max(
                timedelta(seconds=finding["deadline_seconds"]),
                timedelta(minutes=settings["grace_minutes"]),
            )
            previous_alert = _watchdog_last_alert.get(report_type)
            if previous_alert is not None and moment_utc - previous_alert < cooldown:
                logger.debug("watchdog for %s still in cooldown", report_type)
                continue
            _watchdog_last_alert[report_type] = moment_utc

            last = finding["last_success"]
            logger.critical(finding["message"])
            emit_pipeline_failure(
                "watchdog",
                finding["message"],
                report_type=report_type,
                expected_interval_seconds=finding["interval_seconds"],
                grace_seconds=finding["grace_seconds"],
                last_success=last.isoformat(timespec="seconds") if last else None,
                overdue_seconds=finding["overdue_seconds"],
            )
            alerted.append(finding)
    except Exception:
        # A broken watchdog must not take the scheduler with it.
        logger.exception("watchdog check failed")
    return alerted


# ==========================================================================
# Scheduler
# ==========================================================================

_JOB_PREFIX = "towbook:job:"
_INTERNAL_PREFIX = "towbook:internal:"
_CONFIG_WATCH_ID = _INTERNAL_PREFIX + "config-watch"
_WATCHDOG_ID = _INTERNAL_PREFIX + "watchdog"


class _SchedulerState:
    """Tracks the schedule.yaml digest that is currently applied."""

    def __init__(self) -> None:
        self.applied_digest: str = ""
        self.applied_watch_seconds: int = 0
        self.applied_watchdog_minutes: float = 0.0


def job_companies(spec: JobSpec) -> list[str]:
    """The companies one fired job must run for, in roster order.

    A job that names no company runs EVERY enabled company. That is what makes
    one `daily` entry in schedule.yaml serve a multi-tenant install: adding a
    towing company is an edit to companies.yaml, never to the cron table.

    A job pinned to a company that has since been removed or disabled runs
    nothing and says so, rather than silently falling back to somebody else's
    data.
    """
    if spec.company_id:
        company = _companies.get_company(spec.company_id)
        if company is None:
            logger.error(
                "job %s is pinned to company %r, which is not in "
                "config/companies.yaml; the job will not run",
                spec.name,
                spec.company_id,
            )
            emit_pipeline_failure(
                "schedule",
                f"job {spec.name!r} names unknown company {spec.company_id!r}",
                job=spec.name,
                company_id=spec.company_id,
            )
            return []
        if not company.enabled:
            logger.info(
                "job %s is pinned to company %r, which is disabled; skipping",
                spec.name,
                company.id,
            )
            return []
        return [company.id]
    return [company.id for company in _companies.enabled_companies()]


def _run_job(spec: JobSpec, dry_run: bool) -> None:
    """APScheduler entry point for one configured job, for every company.

    The window comes from the clock at fire time, never from "since last run",
    which is what makes the job idempotent: the 15:00 tick always processes
    14:00-15:00, whether it is the first run or the fifth.

    ONE COMPANY'S FAILURE MUST NOT STOP THE OTHERS. Each company gets its own
    try block and its own ``pipeline_failure`` event; the loop continues. A
    tenant whose Towbook password expired cannot take the rest of the roster's
    reporting down with it, and the run that failed is visible on its own
    /health page rather than as an absence everywhere.

    The window is resolved INSIDE each company's scope, because ``resolve_range``
    reads the local timezone and a Texas tenant's "yesterday" is not an Ohio
    tenant's.
    """
    company_ids = job_companies(spec)
    if not company_ids:
        return

    failures: list[str] = []
    for company_id in company_ids:
        try:
            with _companies.use_company(company_id):
                tz = local_timezone()
                now = datetime.now(tz)
                start, end = resolve_range(spec.range_name, now)
                logger.info(
                    "job %s fired at %s for company %s",
                    spec.name,
                    now.isoformat(timespec="seconds"),
                    company_id,
                )
                run_pipeline(
                    spec.report_type,
                    start,
                    end,
                    page_size=spec.page_size,
                    company_id=company_id,
                    dry_run=dry_run,
                    notify=spec.notify,
                    source=spec.source,
                )
        except Exception as exc:
            # run_pipeline never raises by contract, so reaching here means the
            # range failed to resolve or something outside the pipeline broke.
            # Either way it is one company's problem, not the roster's.
            failures.append(company_id)
            logger.exception(
                "job %s failed for company %s; continuing with the rest",
                spec.name,
                company_id,
            )
            emit_pipeline_failure(
                "schedule",
                exc,
                job=spec.name,
                range=spec.range_name,
                company_id=company_id,
            )

    if failures and len(failures) < len(company_ids):
        logger.warning(
            "job %s completed for %d of %d companies; failed: %s",
            spec.name,
            len(company_ids) - len(failures),
            len(company_ids),
            ", ".join(failures),
        )


def _job_ids(scheduler) -> list[str]:
    return [job.id for job in scheduler.get_jobs() if job.id.startswith(_JOB_PREFIX)]


def _prune_jobs(scheduler, wanted: set[str]) -> None:
    """Drop jobs that left schedule.yaml, and collapse duplicate entries.

    ``add_job(replace_existing=True)`` only replaces once the scheduler is
    running; before ``start()`` it appends to the pending list instead. Calling
    ``apply_schedule`` twice before start would therefore leave two pending
    entries per job, and ``remove_job`` deletes only one of them -- so a job
    deleted from the YAML could still be scheduled. Each ``remove_job`` call
    below removes one entry, and the loop repeats until the id is gone.
    """
    for job_id in sorted(set(_job_ids(scheduler)) - wanted):
        logger.info("removing job %s (no longer in schedule.yaml)", job_id)
        while job_id in _job_ids(scheduler):
            try:
                scheduler.remove_job(job_id)
            except Exception:  # pragma: no cover - race with a running job
                logger.debug("job %s already gone", job_id)
                break

    # Keep only the most recently added entry for each surviving id.
    for job_id in sorted(wanted):
        while _job_ids(scheduler).count(job_id) > 1:
            try:
                scheduler.remove_job(job_id)
            except Exception:  # pragma: no cover
                break


def apply_schedule(scheduler, dry_run: bool = False) -> list[JobSpec]:
    """Rebuild the scheduler's job table from config/schedule.yaml.

    Jobs present in the file are added or replaced; jobs that disappeared from
    the file are removed. Internal jobs (config watch, watchdog) are untouched.
    """
    tz = local_timezone()
    specs = load_job_specs(strict=False)
    wanted: set[str] = set()

    for spec in specs:
        job_id = _JOB_PREFIX + spec.name
        if not spec.enabled:
            logger.info("job %s is disabled in schedule.yaml", spec.name)
            continue
        try:
            trigger = cron_trigger(spec.cron, tz)
        except Exception as exc:
            logger.error("job %s has an invalid cron %r: %s", spec.name, spec.cron, exc)
            emit_pipeline_failure("schedule", exc, job=spec.name, cron=spec.cron)
            continue

        scheduler.add_job(
            _run_job,
            trigger=trigger,
            id=job_id,
            name=f"{spec.name} ({spec.report_type})",
            args=[spec, dry_run],
            replace_existing=True,
            coalesce=spec.coalesce,
            max_instances=spec.max_instances,
            misfire_grace_time=spec.misfire_grace_time,
        )
        wanted.add(job_id)
        logger.info(
            "scheduled %s | cron %r | range %s | report %s | page_size %s | "
            "coalesce=%s misfire_grace=%ss",
            spec.name,
            spec.cron,
            spec.range_name,
            spec.report_type,
            spec.page_size or default_page_size(spec.report_type),
            spec.coalesce,
            spec.misfire_grace_time,
        )

    _prune_jobs(scheduler, wanted)

    if not wanted:
        message = "config/schedule.yaml produced no runnable jobs; nothing is scheduled"
        logger.critical(message)
        emit_pipeline_failure("schedule", message, config="config/schedule.yaml")

    return specs


def _config_watch(scheduler, state: _SchedulerState, dry_run: bool) -> None:
    """Rebuild the job table when schedule.yaml changes on disk.

    Hard constraint #2's "zero redeploy" clause: an operator edits the YAML and
    the running scheduler picks it up on the next tick of this job.
    """
    try:
        digest = CONFIG.digest("schedule")
    except ConfigError as exc:
        logger.error("could not read config/schedule.yaml: %s", exc)
        emit_pipeline_failure("schedule", exc, config="config/schedule.yaml")
        return

    if digest == state.applied_digest:
        return

    logger.warning(
        "config/schedule.yaml changed (%s -> %s); rebuilding the job table",
        state.applied_digest[:12] or "none",
        digest[:12],
    )
    try:
        apply_schedule(scheduler, dry_run=dry_run)
        state.applied_digest = digest
        _apply_internal_intervals(scheduler, state)
        _log_next_runs(scheduler)
    except Exception as exc:
        logger.exception("failed to rebuild the job table")
        emit_pipeline_failure("schedule", exc, config="config/schedule.yaml")


def _apply_internal_intervals(scheduler, state: _SchedulerState) -> None:
    """Re-time the internal jobs if their config changed."""
    from apscheduler.triggers.interval import IntervalTrigger

    watch_seconds = _config_watch_seconds()
    if watch_seconds != state.applied_watch_seconds:
        try:
            scheduler.reschedule_job(
                _CONFIG_WATCH_ID, trigger=IntervalTrigger(seconds=watch_seconds)
            )
            state.applied_watch_seconds = watch_seconds
        except Exception:
            logger.debug("config watch job not rescheduled")

    settings = _watchdog_settings()
    minutes = settings["check_every_minutes"]
    if settings["enabled"] and minutes != state.applied_watchdog_minutes:
        try:
            scheduler.reschedule_job(_WATCHDOG_ID, trigger=IntervalTrigger(minutes=minutes))
            state.applied_watchdog_minutes = minutes
        except Exception:
            logger.debug("watchdog job not rescheduled")


def _log_next_runs(scheduler) -> None:
    jobs = {
        job.id: job for job in scheduler.get_jobs() if job.id.startswith(_JOB_PREFIX)
    }
    if not jobs:
        return
    now = now_local()
    lines = []
    for job_id, job in sorted(jobs.items()):
        moment = getattr(job, "next_run_time", None)
        if moment is None:  # not started yet: ask the trigger directly
            try:
                moment = job.trigger.get_next_fire_time(None, now)
            except Exception:  # pragma: no cover - defensive
                moment = None
        lines.append(
            f"    {job.name:<24} next "
            f"{moment.isoformat(timespec='seconds') if moment else 'unscheduled'}"
        )
    logger.info("job table (%d):\n%s", len(jobs), "\n".join(lines))


def _job_error_listener(event) -> None:
    """Last line of defence: a job that raised despite run_pipeline's try/except."""
    exception = getattr(event, "exception", None)
    if exception is None:
        return
    logger.critical("scheduled job %s raised: %s", event.job_id, exception, exc_info=exception)
    emit_pipeline_failure("scheduler", exception, job_id=event.job_id)


def _job_missed_listener(event) -> None:
    """A tick was skipped entirely (process asleep, machine suspended).

    Logged loudly rather than alerted on: coalesce means the next tick still
    covers the current window, and the watchdog owns the "we have actually
    stopped producing reports" decision.
    """
    scheduled = getattr(event, "scheduled_run_time", None)
    logger.error(
        "scheduled job %s MISSED its %s run (outside misfire_grace_time)",
        event.job_id,
        scheduled.isoformat(timespec="seconds") if scheduled else "?",
    )


def build_scheduler(dry_run: bool = False, background: bool = False):
    """Construct a configured scheduler with the job table applied.

    ``background=False`` builds a ``BlockingScheduler`` -- the dedicated
    ``python -m towbook_agent schedule`` process, which is the whole process and
    should own its main thread.

    ``background=True`` builds a ``BackgroundScheduler``, which runs its jobs on
    its own thread pool inside a host process. That is how the web service runs
    the pipeline: the board is the only delivery mechanism now, so a deployment
    that serves pages but schedules nothing would look perfectly healthy while
    quietly going stale. Both share this function, so the two never drift into
    scheduling different things.
    """
    from apscheduler.events import (
        EVENT_JOB_ERROR,
        EVENT_JOB_MISSED,
        EVENT_SCHEDULER_STARTED,
    )
    from apscheduler.triggers.interval import IntervalTrigger

    if background:
        from apscheduler.schedulers.background import BackgroundScheduler as _Scheduler
    else:
        from apscheduler.schedulers.blocking import BlockingScheduler as _Scheduler

    tz = local_timezone()
    scheduler = _Scheduler(
        timezone=tz,
        job_defaults={
            # A missed tick must not stampede: coalesce collapses a burst of
            # catch-up fires into one, and max_instances stops a slow run from
            # overlapping the next one.
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": _DEFAULT_MISFIRE_GRACE,
        },
    )
    scheduler.add_listener(_job_error_listener, EVENT_JOB_ERROR)
    scheduler.add_listener(_job_missed_listener, EVENT_JOB_MISSED)
    scheduler.add_listener(lambda _event: _log_next_runs(scheduler), EVENT_SCHEDULER_STARTED)

    state = _SchedulerState()
    apply_schedule(scheduler, dry_run=dry_run)
    try:
        state.applied_digest = CONFIG.digest("schedule")
    except ConfigError:
        state.applied_digest = ""

    watch_seconds = _config_watch_seconds()
    scheduler.add_job(
        _config_watch,
        trigger=IntervalTrigger(seconds=watch_seconds),
        id=_CONFIG_WATCH_ID,
        name="config watch",
        args=[scheduler, state, dry_run],
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=None,
    )
    state.applied_watch_seconds = watch_seconds

    settings = _watchdog_settings()
    if settings["enabled"]:
        scheduler.add_job(
            _watchdog_job,
            trigger=IntervalTrigger(minutes=settings["check_every_minutes"]),
            id=_WATCHDOG_ID,
            name="watchdog",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=None,
        )
        state.applied_watchdog_minutes = settings["check_every_minutes"]
    else:
        logger.warning(
            "watchdog is disabled in schedule.yaml; a dead pipeline will not announce itself"
        )

    return scheduler


def _watchdog_job() -> None:
    watchdog_check()


# ==========================================================================
# In-process scheduler -- what the deployed web service runs
# ==========================================================================
#
# WHY THE WEB PROCESS SCHEDULES AT ALL
# ------------------------------------
# There is no SMS and no email any more: config/notifications.yaml ships with
# every route disabled and the dashboard IS the delivery. That changes what
# "the scheduler did not run" costs. It used to mean a text did not arrive,
# which is loud. It now means the board keeps rendering yesterday's numbers as
# though they were today's, which is silent -- and the owner is looking at that
# board several times a day.
#
# One service, one process, both jobs, is therefore the safe default: if the
# board is up, the data behind it is being refreshed. RUN_SCHEDULER=false peels
# the scheduler off into its own Railway service (start command
# `python -m towbook_agent schedule`) the day the volume justifies it, with no
# code change.
#
# DOUBLE-RUN GUARD: see towbook_agent/core/leader.py. One uvicorn worker is the
# primary guard; a Postgres advisory lock is the enforcement.

_RUN_SCHEDULER_ENV: str = "RUN_SCHEDULER"

_background_scheduler: Any = None
_background_lease: Any = None

#: How often a process that lost the advisory lock asks for it again.
_LEASE_RETRY_ENV: str = "SCHEDULER_LEASE_RETRY_SECONDS"
_DEFAULT_LEASE_RETRY_SECONDS: float = 60.0

_lease_watcher: Any = None
_lease_watch_stop: Any = None

#: Last thing that went wrong trying to start the scheduler, for /healthz.
#: Railway rate-limits container logs and drops messages, so an error that only
#: exists in a log is an error nobody can retrieve when it matters.
_last_start_error: str | None = None


def lease_watcher_alive() -> bool:
    """Is the take-over thread still running?

    Reported by /healthz so that "not scheduling" can be told apart from "not
    scheduling and nothing is trying to fix it".
    """
    watcher = _lease_watcher
    return bool(watcher is not None and watcher.is_alive())


def _lease_retry_seconds() -> float:
    """Seconds between attempts to take over the scheduler lease.

    Bounded at both ends: below a few seconds this is a busy-poll against
    Postgres for no benefit, and above an hour a failover takes longer than the
    hourly job it exists to protect.
    """
    raw = (os.environ.get(_LEASE_RETRY_ENV) or "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_LEASE_RETRY_SECONDS
    except (TypeError, ValueError):
        logger.warning("ignoring non-numeric %s=%r", _LEASE_RETRY_ENV, raw)
        value = _DEFAULT_LEASE_RETRY_SECONDS
    return max(5.0, min(value, 3600.0))


def _watch_for_the_lease(dry_run: bool) -> None:
    """Keep asking for the lease until this process gets it.

    WHY THIS EXISTS. ``pg_try_advisory_lock`` asked once at boot is not leader
    election, it is a coin toss with no rematch -- and a platform that starts
    the new container before stopping the old one guarantees the new one loses
    it. That is not the "second replica" the warning describes; it is the only
    replica, refused the lock by a container that is about to exit, and then
    never asking again. The scheduler stays dead until somebody restarts the
    service by hand, which is exactly the silent-stale-board failure the whole
    design is meant to prevent.

    On a genuine second replica this loop is close to free -- one query a
    minute -- and it upgrades the guard from "the spare never schedules" to
    "the spare takes over when the leader dies", which is what anyone would
    assume a leader lock already did.
    """
    global _lease_watcher, _lease_watch_stop

    stop = _lease_watch_stop
    interval = _lease_retry_seconds()
    logger.info(
        "another process holds the scheduler lease; retrying every %.0fs so this "
        "process takes over if that one goes away",
        interval,
    )

    # THE TRY GOES INSIDE THE LOOP. With it wrapped around the loop instead, a
    # single transient failure -- one refused connection while Postgres
    # restarts, one blip mid-retry -- ends the thread for the life of the
    # process, and the scheduler never comes back. That is the exact bug this
    # watcher exists to fix, reintroduced one level down. Observed in
    # production: the watcher was gone before the lock it was waiting for
    # became free, so a free lock sat unclaimed.
    failures = 0
    while stop is not None and not stop.wait(interval):
        if _background_scheduler is not None and getattr(_background_scheduler, "running", False):
            break
        try:
            outcome = start_background_scheduler(dry_run=dry_run, _retrying=True)
        except Exception as exc:
            failures += 1
            # Logged sparsely: this runs every interval forever, and a database
            # that is down for an hour must not write sixty identical stack
            # traces into a log the platform is already rate limiting.
            if failures in (1, 5) or failures % 60 == 0:
                logger.error(
                    "scheduler lease retry failed (%d consecutive): %s: %s",
                    failures,
                    type(exc).__name__,
                    exc,
                    exc_info=(failures == 1),
                )
            continue
        failures = 0
        if outcome.get("running"):
            logger.warning(
                "took over the scheduler lease from a process that is no longer "
                "holding it; scheduled jobs are running again in this process"
            )
            break

    _lease_watcher = None


def _ensure_lease_watcher(dry_run: bool) -> None:
    """Start the retry thread once, if it is not already running."""
    global _lease_watcher, _lease_watch_stop

    if _lease_watcher is not None and _lease_watcher.is_alive():
        return
    _lease_watch_stop = threading.Event()
    _lease_watcher = threading.Thread(
        target=_watch_for_the_lease,
        args=(dry_run,),
        name="scheduler-lease-watch",
        daemon=True,
    )
    _lease_watcher.start()


def run_scheduler_enabled() -> bool:
    """Should this process run scheduled jobs? ``RUN_SCHEDULER``, default true.

    Defaults to true on purpose. The failure this guards against -- a board that
    silently stops updating -- is worse than the one an accidental extra runner
    causes, because every job here is idempotent and re-running a window
    recomputes it rather than doubling it.
    """
    raw = (os.environ.get(_RUN_SCHEDULER_ENV) or "").strip().lower()
    if not raw:
        return True
    return raw in _TRUTHY


def start_background_scheduler(dry_run: bool = False, _retrying: bool = False) -> dict[str, Any]:
    """Start the scheduler inside the current process. Never raises.

    Returns a small status dict -- ``{"running", "reason", "jobs", "lease"}`` --
    which the dashboard's ``/health`` view reports, because "is anything actually
    scheduled in this container" is otherwise unanswerable from a browser.
    """
    global _background_scheduler, _background_lease, _process_start, _last_start_error

    status: dict[str, Any] = {"running": False, "reason": "", "jobs": 0, "lease": None}

    if not run_scheduler_enabled():
        status["reason"] = f"{_RUN_SCHEDULER_ENV} is false; this process serves the board only"
        logger.warning(
            "%s -- something else must run `python -m towbook_agent schedule`, or the board "
            "will go stale with no warning other than its own overdue banner.",
            status["reason"],
        )
        return status

    if _background_scheduler is not None and getattr(_background_scheduler, "running", False):
        status.update(running=True, reason="already running")
        return status

    try:
        from .leader import acquire_scheduler_lease

        lease = acquire_scheduler_lease()
        status["lease"] = lease.reason
        if not lease.acquired:
            # Losing the lock is not a terminal state. Keep asking -- the usual
            # holder is the container this deploy is replacing, which is about
            # to exit. See _watch_for_the_lease.
            if not _retrying:
                _ensure_lease_watcher(dry_run)
            status["reason"] = (
                f"{lease.reason}; retrying every {_lease_retry_seconds():.0f}s to take over"
            )
            return status

        _ensure_db()
        # The watchdog measures "has this report been produced lately" against
        # process start when nothing has ever succeeded, so it has to be the
        # start of THIS process, not of the module import.
        _process_start = utcnow()

        scheduler = build_scheduler(dry_run=dry_run, background=True)
        scheduler.start()
        _background_scheduler = scheduler
        _background_lease = lease

        status["running"] = True
        status["jobs"] = len(_job_ids(scheduler))
        status["reason"] = "in-process scheduler running"
        logger.info(
            "in-process scheduler started | timezone %s | %d job(s) | %s | %s",
            timezone_name(),
            status["jobs"],
            lease.reason,
            "DRY RUN - nothing will be sent" if dry_run else "live",
        )
        return status
    except Exception as exc:
        # The board must still come up. A scheduler that failed to start is
        # exactly the condition the overdue banner exists to make visible.
        logger.critical("could not start the in-process scheduler: %s", exc, exc_info=True)
        emit_pipeline_failure("scheduler", exc, phase="startup")
        status["reason"] = f"failed to start: {type(exc).__name__}: {exc}"
        # Kept for /healthz. This exact error was invisible in production for
        # hours because the platform dropped the log line that carried it.
        _last_start_error = f"{type(exc).__name__}: {exc}"
        # Something went wrong on the way up rather than the lock being taken,
        # so keep trying: the usual causes (database not accepting connections
        # yet, config mid-write) are transient.
        if not _retrying:
            _ensure_lease_watcher(dry_run)
        return status


def stop_background_scheduler(wait: bool = False) -> None:
    """Shut the in-process scheduler down and release the lease. Never raises."""
    global _background_scheduler, _background_lease, _lease_watcher, _lease_watch_stop

    # Stop the retry thread FIRST. It exists to start a scheduler, and a
    # shutdown that leaves it running invites it to start one while the process
    # is on its way out -- and to take a lock nothing will ever release.
    stop, _lease_watch_stop = _lease_watch_stop, None
    _lease_watcher = None
    if stop is not None:
        stop.set()

    scheduler, _background_scheduler = _background_scheduler, None
    lease, _background_lease = _background_lease, None

    if scheduler is not None:
        try:
            scheduler.shutdown(wait=wait)
            logger.info("in-process scheduler stopped")
        except Exception as exc:  # pragma: no cover - shutdown races
            logger.debug("scheduler shutdown: %s", exc)
    if lease is not None:
        try:
            lease.release()
        except Exception as exc:  # pragma: no cover
            logger.debug("lease release: %s", exc)


def background_scheduler_status() -> dict[str, Any]:
    """What the in-process scheduler is doing right now, for /health."""
    scheduler = _background_scheduler
    running = bool(scheduler is not None and getattr(scheduler, "running", False))
    jobs: list[dict[str, Any]] = []
    if running:
        try:
            for job in scheduler.get_jobs():
                if not str(job.id).startswith(_JOB_PREFIX):
                    continue
                jobs.append(
                    {
                        "id": job.id,
                        "name": job.name,
                        "next_run_time": getattr(job, "next_run_time", None),
                    }
                )
        except Exception:  # pragma: no cover - defensive
            pass
    return {
        "enabled": run_scheduler_enabled(),
        "running": running,
        "lease": getattr(_background_lease, "reason", None),
        "jobs": sorted(jobs, key=lambda item: item["id"]),
        "retry_watcher_alive": lease_watcher_alive(),
        "last_error": _last_start_error,
    }


def run_scheduler(dry_run: bool = False) -> int:
    """Run the blocking scheduler until interrupted. Returns a process exit code."""
    global _process_start

    _ensure_db()
    _process_start = utcnow()

    scheduler = build_scheduler(dry_run=dry_run)
    settings = _watchdog_settings()

    logger.info(
        "scheduler starting | timezone %s | rules %s | watchdog %s | %s",
        timezone_name(),
        rules_version(),
        f"every {settings['check_every_minutes']:g}m (+{settings['grace_minutes']:g}m grace)"
        if settings["enabled"]
        else "disabled",
        "DRY RUN - nothing will be sent" if dry_run else "live",
    )

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler interrupted; shutting down")
        try:
            scheduler.shutdown(wait=False)
        except Exception:  # pragma: no cover
            pass
        return 0
    except Exception as exc:
        logger.exception("scheduler crashed")
        emit_pipeline_failure("scheduler", exc)
        return 1
    return 0

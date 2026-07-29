"""SQLAlchemy 2.0 models for the Towbook Job Acceptance Intelligence System.

Datetime convention
-------------------
Every timestamp column uses :class:`UTCDateTime`. Values are **stored and
returned as naive datetimes representing UTC**. An aware datetime handed to the
ORM is converted to UTC and stripped of its tzinfo on the way in, so callers can
pass either form without corrupting the data. Use :func:`utcnow` to stamp rows.

Local time (America/Detroit by default) belongs to the presentation layer --
the dashboard, the SMS body and the report windows -- never to storage.

Multi-company
-------------
Every table carries ``company_id``, and it is part of every metrics unique key.
This is not decoration: the system is given to other US Tow Alliance towing
companies, so one install reports on several tenants out of one database.

* ``company_id`` is the id from ``config/companies.yaml``, defaulting to
  ``"default"`` -- the value every row written before the roster existed
  already carries, which is why a single-company install needs no data change.
* **Every metrics unique key leads with it.** ``metrics_daily`` keyed on
  ``date`` alone would have two companies upserting over one another's Tuesday
  and the second one to run would win. Keyed on ``(company_id, date)`` they are
  independent rows.
* ``Request.account_id`` and ``Run.account_id`` remain as SQLAlchemy synonyms
  of ``company_id``. They are the old name for the same thing; keeping them
  means existing callers, fixtures and the ``--account`` CLI flag go on working
  against the renamed column.

Idempotency
-----------
Hard constraint #4: re-running the same window yields the same result.

* ``requests`` is keyed on ``request_id`` and is upserted.
* every metrics table carries a UNIQUE key on its company and window columns
  (``metrics_hourly(company_id, window_start)``, ``metrics_daily(company_id,
  date)``, ``metrics_weekly(company_id, week_start)``,
  ``client_daily(company_id, date, client_key)``), so a recompute upserts
  rather than duplicates.

Immutability
------------
Hard constraint #6: ``Request.service_type_raw`` holds the verbatim source
string and is never mutated. Classification writes to ``service_class``; a
rules change is applied by ``backfill()`` re-deriving ``service_class`` from
the untouched raw value.
"""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, synonym
from sqlalchemy.types import TypeDecorator

__all__ = [
    "Base",
    "UTCDateTime",
    "utcnow",
    "to_utc_naive",
    "client_key_for",
    "company_column",
    "Request",
    "Run",
    "MetricsHourly",
    "MetricsDaily",
    "MetricsWeekly",
    "MetricsMonthly",
    "MetricsMissedWork",
    "ClientDaily",
    "AlertFired",
    "STATUS_VALUES",
    "DEFAULT_ACCOUNT_ID",
    "DEFAULT_COMPANY_ID",
]

#: The controlled status vocabulary. Source strings are mapped onto it by
#: config/schema.yaml -> status_vocabulary, so denied/expired/canceled can be
#: split apart later without a migration.
STATUS_VALUES: tuple[str, ...] = ("accepted", "denied", "expired", "canceled", "pending")

#: The company every row belongs to when no roster is configured. Kept equal to
#: ``core.companies.DEFAULT_COMPANY_ID``; core/companies.py cannot be imported
#: here because it imports the config loader, and models must stay dependency
#: free. tests/test_companies.py asserts the two are the same string.
DEFAULT_COMPANY_ID: str = "default"

#: The old name for the same value. ``account_id`` was what a single-Towbook
#: -login install called it; it survives as an alias so that existing callers,
#: fixtures and the ``--account`` CLI flag keep working.
DEFAULT_ACCOUNT_ID: str = DEFAULT_COMPANY_ID


def company_column() -> Mapped[str]:
    """The ``company_id`` column, identical on every table that carries one.

    Declared once because a tenant column that is NOT NULL on six tables and
    nullable on the seventh is how a row ends up belonging to nobody, and
    because the ``server_default`` is what lets the migration add the column to
    a populated database without a backfill pass.
    """
    return mapped_column(
        String(64),
        nullable=False,
        default=DEFAULT_COMPANY_ID,
        server_default=DEFAULT_COMPANY_ID,
        index=False,
    )


def to_utc_naive(value: datetime | None) -> datetime | None:
    """Normalise a datetime to naive UTC. Naive input is assumed to be UTC."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def utcnow() -> datetime:
    """Current time as a naive UTC datetime, matching the storage convention."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def client_key_for(client_name: str | None) -> str:
    """Derive ``client_key`` from ``client_name``: trimmed and casefolded.

    Defined here so the ingester, the classifier, the metrics aggregator and
    the dashboard all agree on what makes two client names the same client.
    """
    return (client_name or "").strip().casefold()


class UTCDateTime(TypeDecorator):
    """DateTime that accepts aware or naive values and stores naive UTC.

    SQLite has no timezone-aware datetime type, so an aware value would either
    be silently truncated or fail to round-trip. Normalising at the type
    boundary makes the behaviour identical on SQLite and PostgreSQL.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if isinstance(value, datetime):
            return to_utc_naive(value)
        return value

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if isinstance(value, datetime) and value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value


class Base(DeclarativeBase):
    """Declarative base for every model in the system."""

    type_annotation_map = {
        dict[str, Any]: JSON,
        Decimal: Numeric(12, 2),
    }


# --------------------------------------------------------------------------
# requests
# --------------------------------------------------------------------------


class Request(Base):
    """One job offer from the Towbook Digital Requests log.

    Field names are the canonical request record from the spec and must not be
    renamed: every module in the system codes against them.
    """

    __tablename__ = "requests"

    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    #: Which towing company this offer was made to -- an id from
    #: config/companies.yaml. Every read path filters on it.
    company_id: Mapped[str] = company_column()
    #: Deprecated alias for :attr:`company_id`, kept because it is the name the
    #: single-company build used everywhere. A synonym, not a second column:
    #: ``Request.account_id == x`` and ``Request(account_id=x)`` both resolve to
    #: ``company_id``, so no caller had to change.
    account_id = synonym("company_id")
    client_name: Mapped[Optional[str]] = mapped_column(String(255))
    #: trimmed + casefolded client_name -- see client_key_for()
    client_key: Mapped[Optional[str]] = mapped_column(String(255))
    offered_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    responded_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    #: one of STATUS_VALUES
    status: Mapped[Optional[str]] = mapped_column(String(32))
    #: IMMUTABLE IN MEANING, same contract as service_type_raw: the verbatim
    #: source status label ("Goa Approved By Motor Club", "Another Provider
    #: Responded"), stored exactly as the portal wrote it.
    #:
    #: WHY THIS COLUMN EXISTS. ``status`` is a five-value controlled vocabulary,
    #: and collapsing 14 portal statuses onto it destroys the distinctions the
    #: missed-work model is built on: "Expired" (nobody answered) and "Accept
    #: Failed" (we answered and the accept failed) both land on ``expired``, and
    #: "Rejected" (we said no) and "Rejected By Motor Club" (the client pulled
    #: it) are three words apart. Keeping the source string means
    #: ``missed_work.buckets`` can be re-cut in rules.yaml at read time, with no
    #: migration -- exactly the payoff service_type_raw already delivers.
    #:
    #: Unlike service_type_raw this IS overwritten on re-ingest: an offer's
    #: status legitimately changes over time (pending -> accepted, pending ->
    #: expired), so the later pull is the more truthful one.
    status_raw: Mapped[Optional[str]] = mapped_column(Text)
    #: The portal's numeric status code when the source carries one. The JSON
    #: API does; the CSV export does not, so this is NULL on CSV-ingested rows
    #: and ``status_raw`` is what buckets them.
    status_code: Mapped[Optional[int]] = mapped_column(Integer)
    #: raw free text straight from the portal, never normalised in place
    denial_reason: Mapped[Optional[str]] = mapped_column(Text)
    #: IMMUTABLE. The verbatim source service-type string.
    service_type_raw: Mapped[Optional[str]] = mapped_column(Text)
    #: derived from service_type_raw by the classifier; safe to recompute
    service_class: Mapped[Optional[str]] = mapped_column(String(64))
    pickup_location: Mapped[Optional[str]] = mapped_column(Text)
    #: The pickup ZIP as the API states it, NOT parsed back out of
    #: ``pickup_location``. The JSON feed carries it as its own field on 3,122
    #: of 3,124 records; the CSV export does not, so this is NULL on
    #: CSV-ingested rows and territory falls back to the address text there.
    #:
    #: This is what the territory bands in ``rules.yaml -> territory`` are keyed
    #: on. Storing the source field rather than a regex over the address means
    #: an address written "Columbus OH 43201, USA" and one written "43201-1234"
    #: land on the same key, and it keeps the boundary a config decision
    #: instead of a parsing accident.
    pickup_zip: Mapped[Optional[str]] = mapped_column(String(16))
    dropoff_location: Mapped[Optional[str]] = mapped_column(Text)
    #: Towbook's own distance for the job, in miles. VERIFIED populated on
    #: 3,124 of 3,124 records (median 12.2, p90 30.1), and previously discarded.
    #:
    #: Half of what prices a job -- the owner is quoted a hook rate plus
    #: mileage. It is NOT revenue on its own and nothing may total it as money;
    #: see ``amount``, which the API leaves at 0.0 on every record.
    distance_miles: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 1))
    #: When the offer stops being answerable. Stored RAW rather than as a
    #: precomputed window, for the same reason ``status_raw`` is: the derived
    #: value can then be re-cut at read time without a migration.
    #:
    #: ``expires_at - offered_at`` is the decision window, and it is the number
    #: that makes the blind-spot analysis urgent: median 2.8 minutes, mean 3.6,
    #: p90 7.0, max 15.0 across 3,123 records. A missed notification is a lost
    #: job almost immediately. This is NOT a response time -- the feed has no
    #: responded-at field at all (see schema.yaml) -- it is how long the club
    #: gives this company to decide.
    expires_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    truck_assigned: Mapped[Optional[str]] = mapped_column(String(128))
    driver_assigned: Mapped[Optional[str]] = mapped_column(String(128))
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    ingested_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=utcnow)
    source_run_id: Mapped[Optional[str]] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_requests_offered_at", "offered_at"),
        Index("ix_requests_client_key_offered_at", "client_key", "offered_at"),
        Index("ix_requests_status", "status"),
        Index("ix_requests_service_class", "service_class"),
        # The missed-work model buckets on the numeric code first and the
        # verbatim label second, and it does so over whole 7 and 30 day windows.
        Index("ix_requests_status_code", "status_code"),
        # THE MULTI-COMPANY INDEX. Every dashboard query and every metrics
        # window is "this company, this time range", in that order, so the
        # composite is the one that actually gets used.
        Index("ix_requests_company_offered_at", "company_id", "offered_at"),
        # Territory is asked as "this company, was this ZIP ours", so the ZIP
        # index leads with company_id for the same reason the one above does.
        Index("ix_requests_company_zip", "company_id", "pickup_zip"),
        Index("ix_requests_source_run_id", "source_run_id"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Request {self.request_id} {self.status} "
            f"{self.service_class or '?'} client={self.client_key!r}>"
        )


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------


class Run(Base):
    """One execution of the acquire -> ingest pipeline for a window.

    ``status`` is the run lifecycle (started / succeeded / failed / partial),
    not the request status vocabulary. A run that ends in ``failed`` must have
    produced a pipeline_failure event: hard constraint #5, silence is never
    treated as success.
    """

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: Which towing company this run pulled for.
    company_id: Mapped[str] = company_column()
    #: Deprecated alias for :attr:`company_id`; see :class:`Request`.
    account_id = synonym("company_id")
    #: hourly | daily | weekly | manual | seed | backfill
    report_type: Mapped[Optional[str]] = mapped_column(String(32))
    window_start: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    window_end: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    page_size: Mapped[Optional[int]] = mapped_column(Integer)
    #: started | succeeded | failed | partial
    status: Mapped[Optional[str]] = mapped_column(String(32))
    row_count: Mapped[Optional[int]] = mapped_column(Integer)
    rules_version: Mapped[Optional[str]] = mapped_column(String(64))
    started_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    #: path of the archived XLSX under raw/, when there is one
    source_file: Mapped[Optional[str]] = mapped_column(Text)

    __table_args__ = (
        Index("ix_runs_started_at", "started_at"),
        Index("ix_runs_report_type_window_start", "report_type", "window_start"),
        Index("ix_runs_status", "status"),
        # /health answers "did MY company's pipeline run", so the run history
        # is read per company and ordered by time.
        Index("ix_runs_company_started_at", "company_id", "started_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Run {self.run_id} {self.report_type} {self.status} rows={self.row_count}>"


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------


class MetricsHourly(Base):
    """Acceptance metrics for one clock hour, plus the running day totals.

    The running day columns exist because the hourly SMS carries them:

        14:00-14:59 | Offered 12 / Accepted 9 (75%)
        Day: 84 / 61 (73%)
    """

    __tablename__ = "metrics_hourly"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = company_column()
    #: start of the hour, naive UTC; unique WITH company_id -> recompute upserts
    window_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    offered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: accepted / offered in the window, 0.0 when nothing was offered
    rate: Mapped[float] = mapped_column(Numeric(6, 4, asdecimal=False), nullable=False, default=0)
    day_running_offered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    day_running_accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    day_running_rate: Mapped[float] = mapped_column(Numeric(6, 4, asdecimal=False), nullable=False, default=0)
    rules_version: Mapped[Optional[str]] = mapped_column(String(64))
    computed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("company_id", "window_start", name="uq_metrics_hourly_window_start"),
        Index("ix_metrics_hourly_window_start", "window_start"),
        Index("ix_metrics_hourly_company_window_start", "company_id", "window_start"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<MetricsHourly {self.window_start} {self.accepted}/{self.offered}>"


class MetricsDaily(Base):
    """Daily metrics blob, keyed on the local calendar date.

    ``metrics`` is deliberately a JSON document: the daily report grows new
    dimensions (per client, per service class, denial reasons, hour of day)
    without a migration each time.
    """

    __tablename__ = "metrics_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = company_column()
    #: local calendar date, in THIS COMPANY's timezone (companies.yaml, falling
    #: back to the TZ env var). Two companies in different zones legitimately
    #: disagree about where Tuesday ends, which is another reason the unique key
    #: cannot be the date alone.
    date: Mapped[_date] = mapped_column(Date, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    rules_version: Mapped[Optional[str]] = mapped_column(String(64))
    computed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("company_id", "date", name="uq_metrics_daily_date"),
        Index("ix_metrics_daily_date", "date"),
        Index("ix_metrics_daily_company_date", "company_id", "date"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<MetricsDaily {self.date}>"


class MetricsWeekly(Base):
    """Weekly metrics blob, keyed on the Monday that starts the week."""

    __tablename__ = "metrics_weekly"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = company_column()
    #: Monday of the week, local calendar date
    week_start: Mapped[_date] = mapped_column(Date, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    rules_version: Mapped[Optional[str]] = mapped_column(String(64))
    computed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("company_id", "week_start", name="uq_metrics_weekly_week_start"),
        Index("ix_metrics_weekly_week_start", "week_start"),
        Index("ix_metrics_weekly_company_week_start", "company_id", "week_start"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<MetricsWeekly {self.week_start}>"


class MetricsMonthly(Base):
    """Monthly metrics blob, keyed on the 1st of the local calendar month.

    The third cadence, and the one that answers a different question from the
    other two. The daily says what was lost yesterday; the weekly says what to
    change; this says whether what changed is working -- cause growth or decline
    month over month, client trajectories, whether a close-off took effect,
    whether the coverage gap is closing. A month is the shortest window over
    which those are signal rather than weather.

    ``month_start`` is the 1st, so the unique key is a real calendar month and
    a re-run of June upserts June rather than writing it twice.
    """

    __tablename__ = "metrics_monthly"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = company_column()
    #: first day of the month, local calendar date
    month_start: Mapped[_date] = mapped_column(Date, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    rules_version: Mapped[Optional[str]] = mapped_column(String(64))
    computed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("company_id", "month_start", name="uq_metrics_monthly_month_start"),
        Index("ix_metrics_monthly_month_start", "month_start"),
        Index("ix_metrics_monthly_company_month_start", "company_id", "month_start"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<MetricsMonthly {self.month_start}>"


class MetricsMissedWork(Base):
    """The inventory of work we did NOT get, for one window.

    Separate from ``metrics_daily`` / ``metrics_weekly`` on purpose. Those are
    keyed on a calendar date and answer "how did Tuesday go". This table is
    keyed on an arbitrary ``[window_start, window_end)`` span, because the
    question it answers -- *what are we not accepting, and what would it take to
    accept it* -- is asked over whatever window the evidence needs: a day for
    the daily report, a week for the weekly, and 30 days when somebody wants to
    argue about hiring a night dispatcher.

    ``period_type`` is part of the unique key rather than derived from the span,
    so the daily run for Monday and an ad-hoc single-day investigation of the
    same Monday cannot overwrite one another.

    ``metrics`` holds the whole document from
    ``agents.missed_work.compute_missed_work``: totals, by_bucket, by_cause, the
    inventory, the 7x24 blind-spot grid, close-off candidates and the client
    comparison. It is JSON for the same reason the other two are -- the model
    grows dimensions faster than a schema should change -- and it always carries
    ``ranking_basis`` and ``revenue_available`` so no consumer can read a job
    count as a dollar figure.
    """

    __tablename__ = "metrics_missed_work"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = company_column()
    #: inclusive start of the window, naive UTC
    window_start: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    #: EXCLUSIVE end of the window, naive UTC
    window_end: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    #: hourly | daily | weekly | custom -- what asked for this window
    period_type: Mapped[str] = mapped_column(String(32), nullable=False, default="custom")
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    rules_version: Mapped[Optional[str]] = mapped_column(String(64))
    computed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "window_start",
            "window_end",
            "period_type",
            name="uq_metrics_missed_work_window",
        ),
        Index("ix_metrics_missed_work_window_start", "window_start"),
        Index(
            "ix_metrics_missed_work_period_type_window_start",
            "period_type",
            "window_start",
        ),
        Index(
            "ix_metrics_missed_work_company_window_start",
            "company_id",
            "window_start",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<MetricsMissedWork {self.period_type} "
            f"{self.window_start} -> {self.window_end}>"
        )


class ClientDaily(Base):
    """Per-client daily counters -- the input to the client_acceptance_drop alert."""

    __tablename__ = "client_daily"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = company_column()
    date: Mapped[_date] = mapped_column(Date, nullable=False)
    client_key: Mapped[str] = mapped_column(String(255), nullable=False)
    #: preserved for display; client_key is the join key
    client_name: Mapped[Optional[str]] = mapped_column(String(255))
    offered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    denied: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expired: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    canceled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rate: Mapped[float] = mapped_column(Numeric(6, 4, asdecimal=False), nullable=False, default=0)
    rules_version: Mapped[Optional[str]] = mapped_column(String(64))
    computed_at: Mapped[Optional[datetime]] = mapped_column(UTCDateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "company_id", "date", "client_key", name="uq_client_daily_date_client"
        ),
        Index("ix_client_daily_date", "date"),
        Index("ix_client_daily_client_key_date", "client_key", "date"),
        # Two companies both send work to Agero. Without company_id in the key
        # the second one to compute Tuesday would overwrite the first one's
        # Agero row -- same client_key, same date, one row.
        Index("ix_client_daily_company_date", "company_id", "date"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ClientDaily {self.date} {self.client_key} {self.accepted}/{self.offered}>"


# --------------------------------------------------------------------------
# alerts
# --------------------------------------------------------------------------


class AlertFired(Base):
    """An alert that fired.

    ``alert_id`` is the id from config/rules.yaml (for example
    ``client_acceptance_drop``) and ``entity`` is what it fired about -- a
    client_key, a request_id, or an empty string for a global alert. The pair
    drives the ``same_alert_same_entity`` rate limit in notifications.yaml,
    which is why they are indexed together with ``fired_at``.
    """

    __tablename__ = "alerts_fired"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[str] = company_column()
    alert_id: Mapped[str] = mapped_column(String(128), nullable=False)
    entity: Mapped[str] = mapped_column(String(255), nullable=False, default="", server_default="")
    #: low | medium | high (free text; severities are data in rules.yaml)
    severity: Mapped[Optional[str]] = mapped_column(String(32))
    fired_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    acknowledged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    #: set when the alert was suppressed rather than delivered (quiet hours,
    #: rate limit) so the dashboard can show what was held back
    suppressed_reason: Mapped[Optional[str]] = mapped_column(String(64))
    rules_version: Mapped[Optional[str]] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_alerts_fired_alert_entity_fired_at", "alert_id", "entity", "fired_at"),
        Index("ix_alerts_fired_fired_at", "fired_at"),
        Index("ix_alerts_fired_severity", "severity"),
        # The dedupe read is per company: two tenants can both have an
        # `entity` of "agero (swoop)", and one company's alert must never
        # suppress the other's.
        Index(
            "ix_alerts_fired_company_alert_entity",
            "company_id",
            "alert_id",
            "entity",
            "fired_at",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AlertFired {self.alert_id} {self.entity!r} {self.severity} at {self.fired_at}>"

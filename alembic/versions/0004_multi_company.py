"""Multi-company: company_id on every table, and in every metrics unique key.

The system is being given and sold to other US Tow Alliance towing companies,
so one install now reports on several tenants out of one database. That makes
"which company is this row about" a column rather than an assumption.

THREE CHANGES
-------------
1. ``requests.account_id`` and ``runs.account_id`` are RENAMED to
   ``company_id``. Same meaning, same values, one name across the whole schema.
   The ORM keeps ``account_id`` as a SQLAlchemy synonym, so existing callers and
   the ``--account`` CLI flag go on working against the renamed column.

2. ``company_id`` is ADDED to every other table that stores a number:
   metrics_hourly, metrics_daily, metrics_weekly, metrics_monthly,
   metrics_missed_work, client_daily and alerts_fired. All NOT NULL with a
   server default of ``'default'`` -- which is exactly the value every existing
   row already carries in ``requests.account_id``, so a populated database
   upgrades with no backfill pass and no ambiguity about who owns what.

3. **Every metrics unique key is re-cut to lead with company_id.** This is the
   change that matters. ``metrics_daily`` keyed on ``date`` alone means two
   companies upsert over one another's Tuesday and whichever ran second wins --
   silently, with a plausible-looking number. The same applies to
   ``metrics_hourly.window_start``, ``metrics_weekly.week_start``,
   ``metrics_monthly.month_start``, ``client_daily(date, client_key)`` (two
   tenants both send work to Agero) and the missed-work window key.

Composite ``(company_id, <time>)`` indexes are added alongside, because every
read in the system is now "this company, this range", in that order.

SQLite cannot ALTER a UNIQUE constraint, so each affected table is rebuilt
through ``batch_alter_table``. On PostgreSQL the same calls become ordinary
DDL. Constraint NAMES are unchanged so the migration and the ORM metadata keep
describing the same object; only their column lists grow.

Revision ID: 0004
Revises: 0003
Created: 2026-07-28

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The company every pre-existing row belongs to. Equal to
#: ``core.models.DEFAULT_COMPANY_ID`` and to the ``account_id`` default the
#: original schema shipped with, which is what makes this upgrade a rename
#: rather than a data migration.
DEFAULT_COMPANY = "default"


def _company_column() -> sa.Column:
    return sa.Column(
        "company_id",
        sa.String(length=64),
        nullable=False,
        server_default=DEFAULT_COMPANY,
    )


#: ``table -> (unique constraint name, columns AFTER company_id)``. Every one
#: of these keys gains company_id at the front.
_UNIQUE_KEYS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("metrics_hourly", "uq_metrics_hourly_window_start", ("window_start",)),
    ("metrics_daily", "uq_metrics_daily_date", ("date",)),
    ("metrics_weekly", "uq_metrics_weekly_week_start", ("week_start",)),
    ("metrics_monthly", "uq_metrics_monthly_month_start", ("month_start",)),
    (
        "metrics_missed_work",
        "uq_metrics_missed_work_window",
        ("window_start", "window_end", "period_type"),
    ),
    ("client_daily", "uq_client_daily_date_client", ("date", "client_key")),
)

#: ``index name -> (table, columns)`` for the composite reads this enables.
_COMPANY_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_runs_company_started_at", "runs", ("company_id", "started_at")),
    (
        "ix_metrics_hourly_company_window_start",
        "metrics_hourly",
        ("company_id", "window_start"),
    ),
    ("ix_metrics_daily_company_date", "metrics_daily", ("company_id", "date")),
    (
        "ix_metrics_weekly_company_week_start",
        "metrics_weekly",
        ("company_id", "week_start"),
    ),
    (
        "ix_metrics_monthly_company_month_start",
        "metrics_monthly",
        ("company_id", "month_start"),
    ),
    (
        "ix_metrics_missed_work_company_window_start",
        "metrics_missed_work",
        ("company_id", "window_start"),
    ),
    ("ix_client_daily_company_date", "client_daily", ("company_id", "date")),
    (
        "ix_alerts_fired_company_alert_entity",
        "alerts_fired",
        ("company_id", "alert_id", "entity", "fired_at"),
    ),
)


def upgrade() -> None:
    # ----------------------------------------------------------------------
    # 1. requests / runs: rename account_id -> company_id.
    #
    # The index on (account_id, offered_at) is dropped first and recreated
    # under its new name afterwards: SQLite's batch rebuild would otherwise
    # carry the old index name onto the new column, leaving a schema whose
    # index names lie about their columns.
    # ----------------------------------------------------------------------
    op.drop_index("ix_requests_account_offered_at", table_name="requests")
    with op.batch_alter_table("requests") as batch:
        batch.alter_column(
            "account_id",
            new_column_name="company_id",
            existing_type=sa.String(length=64),
            existing_nullable=False,
            existing_server_default=DEFAULT_COMPANY,
        )
    op.create_index(
        "ix_requests_company_offered_at", "requests", ["company_id", "offered_at"]
    )

    with op.batch_alter_table("runs") as batch:
        batch.alter_column(
            "account_id",
            new_column_name="company_id",
            existing_type=sa.String(length=64),
            existing_nullable=False,
            existing_server_default=DEFAULT_COMPANY,
        )

    # ----------------------------------------------------------------------
    # 2 + 3. Every other table gains company_id, and every metrics unique key
    # is re-cut to lead with it.
    #
    # recreate="always" because on SQLite dropping a named UNIQUE constraint
    # is only possible by rebuilding the table, and doing the add_column in the
    # same batch means one rebuild rather than two.
    # ----------------------------------------------------------------------
    for table, constraint, columns in _UNIQUE_KEYS:
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.add_column(_company_column())
            batch.drop_constraint(constraint, type_="unique")
            batch.create_unique_constraint(constraint, ["company_id", *columns])

    # alerts_fired has no unique constraint -- the notifier's rate limit is a
    # time-window read, not a key -- so it is a plain add_column.
    op.add_column("alerts_fired", _company_column())

    for name, table, columns in _COMPANY_INDEXES:
        op.create_index(name, table, list(columns))


def downgrade() -> None:
    for name, table, _columns in reversed(_COMPANY_INDEXES):
        op.drop_index(name, table_name=table)

    op.drop_column("alerts_fired", "company_id")

    for table, constraint, columns in reversed(_UNIQUE_KEYS):
        with op.batch_alter_table(table, recreate="always") as batch:
            batch.drop_constraint(constraint, type_="unique")
            batch.drop_column("company_id")
            batch.create_unique_constraint(constraint, list(columns))

    with op.batch_alter_table("runs") as batch:
        batch.alter_column(
            "company_id",
            new_column_name="account_id",
            existing_type=sa.String(length=64),
            existing_nullable=False,
            existing_server_default=DEFAULT_COMPANY,
        )

    op.drop_index("ix_requests_company_offered_at", table_name="requests")
    with op.batch_alter_table("requests") as batch:
        batch.alter_column(
            "company_id",
            new_column_name="account_id",
            existing_type=sa.String(length=64),
            existing_nullable=False,
            existing_server_default=DEFAULT_COMPANY,
        )
    op.create_index(
        "ix_requests_account_offered_at", "requests", ["account_id", "offered_at"]
    )

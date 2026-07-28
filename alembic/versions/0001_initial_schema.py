"""Initial schema: requests, runs, metrics, client_daily, alerts_fired.

Creates every table, every index and every unique constraint the models
declare. The unique keys on the metrics window columns are load bearing, not
decoration: hard constraint #4 says re-running a window must yield the same
result, and they are what turns a recompute into an upsert instead of a
duplicate row.

Revision ID: 0001
Revises:
Created: 2026-07-28

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ----------------------------------------------------------------------
    # requests -- the canonical request record. Field names are fixed by the
    # spec; every module in the system codes against them.
    # ----------------------------------------------------------------------
    op.create_table(
        "requests",
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column(
            "account_id",
            sa.String(length=64),
            nullable=False,
            server_default="default",
        ),
        sa.Column("client_name", sa.String(length=255), nullable=True),
        # trimmed + casefolded client_name
        sa.Column("client_key", sa.String(length=255), nullable=True),
        sa.Column("offered_at", sa.DateTime(), nullable=True),
        sa.Column("responded_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("denial_reason", sa.Text(), nullable=True),
        # IMMUTABLE: the verbatim source string, never rewritten
        sa.Column("service_type_raw", sa.Text(), nullable=True),
        sa.Column("service_class", sa.String(length=64), nullable=True),
        sa.Column("pickup_location", sa.Text(), nullable=True),
        sa.Column("dropoff_location", sa.Text(), nullable=True),
        sa.Column("truck_assigned", sa.String(length=128), nullable=True),
        sa.Column("driver_assigned", sa.String(length=128), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("ingested_at", sa.DateTime(), nullable=True),
        sa.Column("source_run_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index("ix_requests_offered_at", "requests", ["offered_at"])
    op.create_index(
        "ix_requests_client_key_offered_at", "requests", ["client_key", "offered_at"]
    )
    op.create_index("ix_requests_status", "requests", ["status"])
    op.create_index("ix_requests_service_class", "requests", ["service_class"])
    op.create_index(
        "ix_requests_account_offered_at", "requests", ["account_id", "offered_at"]
    )
    op.create_index("ix_requests_source_run_id", "requests", ["source_run_id"])

    # ----------------------------------------------------------------------
    # runs -- one acquire -> ingest execution. A run that ends `failed` must
    # have emitted a pipeline_failure event.
    # ----------------------------------------------------------------------
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column(
            "account_id",
            sa.String(length=64),
            nullable=False,
            server_default="default",
        ),
        sa.Column("report_type", sa.String(length=32), nullable=True),
        sa.Column("window_start", sa.DateTime(), nullable=True),
        sa.Column("window_end", sa.DateTime(), nullable=True),
        sa.Column("page_size", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("rules_version", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("source_file", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_runs_started_at", "runs", ["started_at"])
    op.create_index(
        "ix_runs_report_type_window_start", "runs", ["report_type", "window_start"]
    )
    op.create_index("ix_runs_status", "runs", ["status"])

    # ----------------------------------------------------------------------
    # metrics_hourly -- one row per clock hour, plus the running day totals
    # the hourly SMS carries.
    # ----------------------------------------------------------------------
    op.create_table(
        "metrics_hourly",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("offered", sa.Integer(), nullable=False),
        sa.Column("accepted", sa.Integer(), nullable=False),
        sa.Column("rate", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("day_running_offered", sa.Integer(), nullable=False),
        sa.Column("day_running_accepted", sa.Integer(), nullable=False),
        sa.Column("day_running_rate", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("rules_version", sa.String(length=64), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("window_start", name="uq_metrics_hourly_window_start"),
    )
    op.create_index("ix_metrics_hourly_window_start", "metrics_hourly", ["window_start"])

    # ----------------------------------------------------------------------
    # metrics_daily / metrics_weekly -- JSON blobs so a new report dimension
    # does not need a migration every time.
    # ----------------------------------------------------------------------
    op.create_table(
        "metrics_daily",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("rules_version", sa.String(length=64), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("date", name="uq_metrics_daily_date"),
    )
    op.create_index("ix_metrics_daily_date", "metrics_daily", ["date"])

    op.create_table(
        "metrics_weekly",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("rules_version", sa.String(length=64), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("week_start", name="uq_metrics_weekly_week_start"),
    )
    op.create_index("ix_metrics_weekly_week_start", "metrics_weekly", ["week_start"])

    # ----------------------------------------------------------------------
    # client_daily -- the input to client_acceptance_drop.
    # ----------------------------------------------------------------------
    op.create_table(
        "client_daily",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("client_key", sa.String(length=255), nullable=False),
        sa.Column("client_name", sa.String(length=255), nullable=True),
        sa.Column("offered", sa.Integer(), nullable=False),
        sa.Column("accepted", sa.Integer(), nullable=False),
        sa.Column("denied", sa.Integer(), nullable=False),
        sa.Column("expired", sa.Integer(), nullable=False),
        sa.Column("canceled", sa.Integer(), nullable=False),
        sa.Column("rate", sa.Numeric(precision=6, scale=4), nullable=False),
        sa.Column("rules_version", sa.String(length=64), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("date", "client_key", name="uq_client_daily_date_client"),
    )
    op.create_index("ix_client_daily_date", "client_daily", ["date"])
    op.create_index(
        "ix_client_daily_client_key_date", "client_daily", ["client_key", "date"]
    )

    # ----------------------------------------------------------------------
    # alerts_fired -- what fired, about what, and whether it was held back.
    # (alert_id, entity, fired_at) is the rate limit lookup.
    # ----------------------------------------------------------------------
    op.create_table(
        "alerts_fired",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("alert_id", sa.String(length=128), nullable=False),
        sa.Column("entity", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("severity", sa.String(length=32), nullable=True),
        sa.Column("fired_at", sa.DateTime(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "acknowledged", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("suppressed_reason", sa.String(length=64), nullable=True),
        sa.Column("rules_version", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_alerts_fired_alert_entity_fired_at",
        "alerts_fired",
        ["alert_id", "entity", "fired_at"],
    )
    op.create_index("ix_alerts_fired_fired_at", "alerts_fired", ["fired_at"])
    op.create_index("ix_alerts_fired_severity", "alerts_fired", ["severity"])


def downgrade() -> None:
    op.drop_index("ix_alerts_fired_severity", table_name="alerts_fired")
    op.drop_index("ix_alerts_fired_fired_at", table_name="alerts_fired")
    op.drop_index("ix_alerts_fired_alert_entity_fired_at", table_name="alerts_fired")
    op.drop_table("alerts_fired")

    op.drop_index("ix_client_daily_client_key_date", table_name="client_daily")
    op.drop_index("ix_client_daily_date", table_name="client_daily")
    op.drop_table("client_daily")

    op.drop_index("ix_metrics_weekly_week_start", table_name="metrics_weekly")
    op.drop_table("metrics_weekly")

    op.drop_index("ix_metrics_daily_date", table_name="metrics_daily")
    op.drop_table("metrics_daily")

    op.drop_index("ix_metrics_hourly_window_start", table_name="metrics_hourly")
    op.drop_table("metrics_hourly")

    op.drop_index("ix_runs_status", table_name="runs")
    op.drop_index("ix_runs_report_type_window_start", table_name="runs")
    op.drop_index("ix_runs_started_at", table_name="runs")
    op.drop_table("runs")

    op.drop_index("ix_requests_source_run_id", table_name="requests")
    op.drop_index("ix_requests_account_offered_at", table_name="requests")
    op.drop_index("ix_requests_service_class", table_name="requests")
    op.drop_index("ix_requests_status", table_name="requests")
    op.drop_index("ix_requests_client_key_offered_at", table_name="requests")
    op.drop_index("ix_requests_offered_at", table_name="requests")
    op.drop_table("requests")

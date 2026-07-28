"""Missed-work model: metrics_missed_work, requests.status_raw/status_code.

Two changes, both in service of MISSED_WORK_MODEL.md.

1. ``requests.status_raw`` and ``requests.status_code``.
   ``requests.status`` is a five-value controlled vocabulary. The portal emits
   fourteen statuses, and collapsing them destroys the exact distinctions the
   missed-work model is built on:

       "Expired"                     -> nobody answered      (no_response)
       "Accept Failed"               -> we answered, it failed (accept_failed)
       "Rejected"                    -> WE said no            (declined)
       "Rejected By Motor Club"      -> the CLIENT pulled it  (client_withdrew)

   The first pair both map to ``expired`` and the second pair are three words
   apart. Keeping the verbatim source label -- and the numeric code where the
   source has one -- means ``rules.yaml -> missed_work.buckets`` can be re-cut
   at read time with no migration, which is exactly the payoff
   ``service_type_raw`` already delivers for service classification.

   Both columns are nullable. Rows ingested before this migration keep NULL and
   fall back to bucketing on the canonical ``status``; nothing has to be
   re-ingested for the reports to work, and re-ingesting fills them in.

2. ``metrics_missed_work``. Keyed on an arbitrary
   ``(window_start, window_end, period_type)`` span rather than a calendar date,
   because "what are we not accepting" is asked over whatever window the
   evidence needs. The unique key is what makes a recompute an upsert rather
   than a duplicate (hard constraint #4).

Revision ID: 0002
Revises: 0001
Created: 2026-07-28

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ----------------------------------------------------------------------
    # requests: keep the source status, not only the collapsed one.
    # ----------------------------------------------------------------------
    op.add_column("requests", sa.Column("status_raw", sa.Text(), nullable=True))
    op.add_column("requests", sa.Column("status_code", sa.Integer(), nullable=True))
    op.create_index("ix_requests_status_code", "requests", ["status_code"])

    # ----------------------------------------------------------------------
    # metrics_missed_work: the inventory of work we did not get.
    # ----------------------------------------------------------------------
    op.create_table(
        "metrics_missed_work",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        # EXCLUSIVE end
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("period_type", sa.String(length=32), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("rules_version", sa.String(length=64), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "window_start",
            "window_end",
            "period_type",
            name="uq_metrics_missed_work_window",
        ),
    )
    op.create_index(
        "ix_metrics_missed_work_window_start", "metrics_missed_work", ["window_start"]
    )
    op.create_index(
        "ix_metrics_missed_work_period_type_window_start",
        "metrics_missed_work",
        ["period_type", "window_start"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_metrics_missed_work_period_type_window_start",
        table_name="metrics_missed_work",
    )
    op.drop_index(
        "ix_metrics_missed_work_window_start", table_name="metrics_missed_work"
    )
    op.drop_table("metrics_missed_work")

    op.drop_index("ix_requests_status_code", table_name="requests")
    op.drop_column("requests", "status_code")
    op.drop_column("requests", "status_raw")

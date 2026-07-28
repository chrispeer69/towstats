"""metrics_monthly -- the third cadence.

The owner reviews daily, decides weekly, and steers monthly. The first two
already have a table; this adds the third.

Deliberately a separate table rather than a ``period_type`` column on
``metrics_weekly``: the unique key is what makes a re-run an upsert (hard
constraint #4), and a month and a week are different keys over the same rows.
``month_start`` is always the 1st of the local calendar month, so re-running
June writes June again rather than a second June.

Same shape as metrics_daily / metrics_weekly, and for the same reason: the
document is JSON because the report grows dimensions -- month-over-month cause
deltas, client trajectories, close-off effectiveness, coverage trend -- faster
than a schema should change.

Revision ID: 0003
Revises: 0002
Created: 2026-07-28

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "metrics_monthly",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        # first day of the local calendar month
        sa.Column("month_start", sa.Date(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column("rules_version", sa.String(length=64), nullable=True),
        sa.Column("computed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("month_start", name="uq_metrics_monthly_month_start"),
    )
    op.create_index("ix_metrics_monthly_month_start", "metrics_monthly", ["month_start"])


def downgrade() -> None:
    op.drop_index("ix_metrics_monthly_month_start", table_name="metrics_monthly")
    op.drop_table("metrics_monthly")

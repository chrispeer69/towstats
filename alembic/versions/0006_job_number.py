"""Store the Towbook job / call number the API already sends.

``callNumber`` arrives on every JSON record and has been discarded since the
API path was built -- it sat in ``schema.yaml`` under ``optional_keys``,
recognised as expected and mapped to nothing. It is the reference the owner
actually uses: the number typed into Towbook to pull a job back up while
reviewing why it went the way it did.

WHAT IT IS, AND WHAT IT IS NOT
------------------------------
``requests.job_number`` -- TEXT, because it is an identifier and not a
    quantity. Nothing in this system may sum, average or rank it.

    MEASURED over the 3,124 archived records in ``raw/``:

        accepted        1,074 / 1,085  (99%)
        NOT accepted      319 / 2,039  (16%)
          Expired             0 / 817
          Rejected            1 / 428
          Another Provider    0 /  25
          Goa Approved       24 /  24   <- accepted first, lost afterwards
          Service Failure    25 /  25   <- likewise

    Towbook issues the number when an offer becomes a job, so the rows that
    carry one are very nearly "the jobs we took". This is a fact about the
    portal, not a gap in the mapping, and it is the reason the read paths pair
    it with ``request_id``: the Digital Request id (the API's callRequestId) is
    populated on 3,124 of 3,124 records and is what identifies an offer we
    never accepted. ``towbook_ref`` -- job number when there is one, request id
    when there is not -- is computed at read time in ``agents/metrics.py`` and
    ``web/queries.py``, deliberately NOT stored, so the day Towbook starts
    numbering unanswered offers the reports improve with no migration.

    A source value of ``0`` is NOT a job number. Towbook sends ``callNumber:
    0`` rather than omitting the key, and ``schema.yaml ->
    value_cleanup.zero_means_absent`` turns it into NULL at ingest. Without it
    1,731 rows would report job "0" -- a reference somebody types into Towbook,
    gets nothing back for, and afterwards stops trusting the report.

    NEVER AN IDENTITY. ``request_id`` remains the primary key. This value is
    written after an offer is answered, so keying on it would give one offer
    two different keys across two pulls and count it twice -- inflating the
    offered denominator this system exists to get right.

BACKFILL
--------
None here. The column is nullable, so a populated database upgrades instantly
and existing rows read NULL until their window is re-ingested. Ingestion
upserts on ``request_id``, so re-running a past window against the archived
payloads under ``raw/`` fills the column in without double-counting anything.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable add_column: no key touched, no table rebuild, no
    # batch_alter_table dance on SQLite.
    op.add_column("requests", sa.Column("job_number", sa.String(length=64), nullable=True))

    # "Here is a job number, show me the offer behind it" is a lookup, and it is
    # asked per company -- two tenants number their jobs independently, so the
    # index leads with company_id like every other read path since 0004.
    op.create_index(
        "ix_requests_company_job_number", "requests", ["company_id", "job_number"]
    )


def downgrade() -> None:
    op.drop_index("ix_requests_company_job_number", table_name="requests")
    op.drop_column("requests", "job_number")

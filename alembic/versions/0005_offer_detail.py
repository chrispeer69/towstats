"""Store the pickup ZIP, the distance and the offer expiry the API already sends.

Three fields arrive on every JSON record and have been discarded at ingestion
since the API path was built. They were listed in ``schema.yaml`` under
``optional_keys`` -- recognised as expected, mapped to nothing -- and the
comment there said promoting one later would be a one-line change. This is
that change, plus the columns to put them in.

WHAT THEY ARE, AND WHY EACH ONE EARNS A COLUMN
----------------------------------------------
``requests.pickup_zip`` -- VERIFIED on 3,122 of 3,124 archived records.
    The territory question ("was this even our job to take?") is decided on
    ZIP, against the bands in ``rules.yaml -> territory``. The API states the
    ZIP as its own field; parsing it back out of ``pickup_location`` would mean
    a regex over free text written at least six different ways, and a boundary
    that moves when an address is formatted differently is not a boundary.
    NULL on CSV-ingested rows -- that export carries no ZIP field.

``requests.distance_miles`` -- VERIFIED on 3,124 of 3,124 (median 12.2, p90 30.1).
    Half of what prices a job: the owner is quoted a hook rate PLUS mileage.
    NOT revenue, and nothing may total it as money -- ``amount`` is 0.0 on
    every record this API has ever returned.

``requests.expires_at`` -- VERIFIED on every record.
    Stored raw rather than as a precomputed window, for the same reason
    ``status_raw`` is stored raw: the derived value can be re-cut at read time
    with no migration. ``expires_at - offered_at`` is the decision window, and
    it is the evidence behind MISSED_WORK_MODEL.md section 4 -- median 2.8
    minutes, mean 3.6, p90 7.0, max 15.0 across 3,123 records. A missed
    notification is a lost job almost immediately.

    It is NOT a response time. This feed has no responded-at field at all;
    ``schema.yaml`` maps ``responded_at`` to nothing on purpose, and
    approximating it from this column would corrupt the one number the
    blind-spot analysis rests on.

BACKFILL
--------
None here. All three are nullable, so a populated database upgrades instantly
and existing rows read NULL until their window is re-ingested. Every job is
idempotent -- ingestion upserts on ``callRequestId`` -- so re-running a past
window against the archived payloads under ``raw/`` fills them in without
double-counting anything.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain add_column x3: all nullable, no key or index touched, so there is
    # no table rebuild and no batch_alter_table dance on SQLite.
    op.add_column("requests", sa.Column("pickup_zip", sa.String(length=16), nullable=True))
    op.add_column("requests", sa.Column("distance_miles", sa.Numeric(6, 1), nullable=True))
    op.add_column("requests", sa.Column("expires_at", sa.DateTime(), nullable=True))

    # Territory is read as "this company, these ZIPs, this range", so the ZIP
    # index leads with company_id like every other read path added in 0004.
    op.create_index("ix_requests_company_zip", "requests", ["company_id", "pickup_zip"])


def downgrade() -> None:
    op.drop_index("ix_requests_company_zip", table_name="requests")
    op.drop_column("requests", "expires_at")
    op.drop_column("requests", "distance_miles")
    op.drop_column("requests", "pickup_zip")

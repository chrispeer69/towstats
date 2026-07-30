"""Store the vehicle, so a re-offered job can be recognised as the same job.

``vehicle`` arrives on every record and has been discarded since the API path
was built. It is promoted here for one reason: it is the ONLY field in this
feed that identifies the customer's job.

THERE IS NO CUSTOMER NAME IN THIS FEED
--------------------------------------
All 30 keys the API returns were enumerated against the 3,124 archived
records. The offering motor club is there (``providerName``), the address is
there (``startingLocation``), the car is there -- the person is not. So "the
same customer's job, offered again" can only be expressed as "the same club,
the same car, inside a short window".

WHY NOT THE ADDRESS
-------------------
Because it is free text and the clubs rewrite it. Of the 18 pairs where the
same car was offered twice by the same club within an hour from a
*different-looking* address, all 18 were plainly one job:

    "I-270 N, Dublin, OH, 43017"          vs "I-270, Dublin, OH, USA 43017"
    "9750 Innovation Campus Way New Albany" vs "9750 Innovation Campus Wy, Johnstown"
    "617-619 S 3rd St, Columbus"          vs "615 S 3rd St, Columbus"

Keying on the address would have split every one of those back into two
offers. ``vehicle`` is 2,481 distinct strings across 3,124 records and does
not suffer from it.

WHAT THIS IS WORTH
------------------
228 clusters over 30 days, 261 offers suppressed -- 8.4% of everything the
account was offered. They are overwhelmingly the club asking twice after we
did not answer: Cancelled -> Cancelled (56), Expired -> Expired (44),
Rejected -> Rejected (29). Counted as written, they inflate both the offered
denominator and the decline count, which is exactly the skew the rule exists
to remove. See config/rules.yaml -> duplicate_offers.

NOT AN IDENTITY, AND THE FINGERPRINT IS UNCHANGED
-------------------------------------------------
``schema.yaml -> identity.fallback_source_columns`` already lists "Vehicle" as
one of the raw columns that separates two genuinely different CSV rows. That
list is DELIBERATELY untouched here. Changing the fingerprint recipe would
give every CSV-ingested offer a new request_id on the next pull and duplicate
the entire history -- the precise failure this migration exists to reduce.

BACKFILL
--------
None here. Nullable, so a populated database upgrades instantly. A row with no
vehicle is never treated as a duplicate of anything (duplicates.py refuses to
key on a blank), so an un-backfilled history simply behaves the way it does
today until its window is re-ingested from the archives under raw/.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable add_column, no key or index touched. Deliberately NOT indexed:
    # duplicate detection groups a window's rows in memory, it never looks a
    # vehicle up, so an index would be write cost for no read.
    op.add_column("requests", sa.Column("vehicle", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("requests", "vehicle")

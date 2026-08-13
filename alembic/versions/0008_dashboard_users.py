"""Accounts, so that a company on a shared install cannot read another's board.

WHAT WAS WRONG
--------------
The dashboard was one shared password with full access to the whole roster.
That is exactly what was asked for, and it was right for the install this
system was built on: one owner, two of his own legal entities, one password
between them. It stops being right the moment a company that does NOT own the
server is added to config/companies.yaml -- because the company switcher is a
preference, not a permission, and `/company/<somebody-elses-id>` answered.

WHAT THIS ADDS
--------------
One table. A row is a person who may sign in, a password digest, and the list
of companies they may open. `core.companies.use_visible_companies` turns that
list into a filter the whole registry obeys, so the enforcement is not spread
across forty endpoints: a company outside the scope cannot be listed in the
switcher, cannot be resolved by id, and is not a member of the merged view.

NOTHING CHANGES UNTIL AN ACCOUNT EXISTS
---------------------------------------
An empty table means shared-password mode -- DASHBOARD_PASSWORD, full access,
precisely today's behaviour. The single-company installs already running do not
notice this migration, and there is no flag to set: creating the first account
is what switches the install over, and deleting the last one switches it back.

BACKFILL
--------
None. The table is created empty on purpose. A migration that invented an
`admin` account with a known password would put a live credential into every
deployment of this repository, which is the failure this whole change exists to
end. The first account is created deliberately -- from DASHBOARD_ADMIN_USER /
DASHBOARD_ADMIN_PASS at boot, or with `python -m towbook_agent users add`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dashboard_users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "display_name",
            sa.String(length=128),
            nullable=False,
            server_default="",
        ),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="member"),
        # The companies this reader may open. Empty for an operator, who sees
        # every company by role rather than by enumeration -- see the note on
        # DashboardUser about why that is not stored as a wildcard.
        sa.Column("company_scope", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column(
            "must_change_password", sa.Boolean(), nullable=False, server_default="0"
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        # Part of the session signature, so moving it logs this user out
        # everywhere. See web/auth.py.
        sa.Column("password_changed_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_dashboard_users_username"),
    )
    op.create_index("ix_dashboard_users_enabled", "dashboard_users", ["enabled"])


def downgrade() -> None:
    # Dropping this table returns the install to the shared password. It does
    # not weaken anything that was not already so before the upgrade, but it
    # DELETES every account and every company scope with them, and there is no
    # way back: the digests are not recoverable and the scopes are not written
    # down anywhere else.
    op.drop_index("ix_dashboard_users_enabled", table_name="dashboard_users")
    op.drop_table("dashboard_users")

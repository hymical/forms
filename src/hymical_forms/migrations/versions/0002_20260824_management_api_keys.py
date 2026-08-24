"""
management api keys

Adds the credential table the management authentication boundary resolves keys
against. Nothing the baseline created is touched, so a database already holding
endpoints, submissions, deliveries and attempts gains a table and loses nothing,
and the downgrade removes exactly that table again.

The credential itself has no column here. Only a digest of it is stored, so a
copy of this table is not a set of working keys.

Timestamps are written as ``sa.DateTime(timezone=True)`` rather than the
application's ``UtcDateTime`` decorator, for the reason given in ``0001``: a
migration is a frozen record of a change, not a view of the current models.

revision: 0002
revises: 0001
created: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    create the management api key table
    """
    op.create_table(
        "management_api_keys",
        sa.Column("id", sa.String(length=35), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("display_prefix", sa.String(length=17), nullable=False),
        sa.Column("key_digest", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_management_api_keys")),
        # Authentication resolves a candidate by digest, so this is both the
        # uniqueness guarantee and the index every management request rides on.
        sa.UniqueConstraint("key_digest", name="uq_management_api_keys_key_digest"),
    )


def downgrade() -> None:
    """
    remove the schema this revision created
    """
    # Nothing else references this table, so dropping it leaves everything the
    # baseline created exactly as it was.
    op.drop_table("management_api_keys")

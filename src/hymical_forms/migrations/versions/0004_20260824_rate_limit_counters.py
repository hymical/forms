"""
public ingestion rate limit counters

Adds the table the two public ingestion limiters count in. Nothing an earlier
revision created is touched, so a database already holding endpoints,
submissions, deliveries, attempts and management keys gains a table and loses
nothing, and the downgrade removes exactly that table again.

The identity of a counter is its limiter, its subject and the start of the fixed
window it counts, and all three are the primary key. That is deliberate: the
increment is written as an upsert, so the index the primary key already creates
is the index the conflict resolves on, and there is no second structure to keep
in agreement with it. The window column is indexed on its own as well, because
cleanup ranges over it without knowing a limiter or a subject.

Nothing in this table is durable in the sense the rest of the schema is. Every
row stops being consulted the moment its window ends, and losing the whole table
costs at most one window of accounting, which is why the downgrade drops it
without a backfill and why deleting old rows in bulk is safe.

No raw address is stored here. The per-IP subject is a digest, and no submitted
field name, field value or credential reaches this table at all.

Timestamps are written as ``sa.DateTime(timezone=True)`` rather than the
application's ``UtcDateTime`` decorator, for the reason given in ``0001``: a
migration is a frozen record of a change, not a view of the current models.

revision: 0004
revises: 0003
created: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    create the table the public ingestion limiters count in
    """
    op.create_table(
        "rate_limit_counters",
        sa.Column("limiter", sa.String(length=16), nullable=False),
        sa.Column("subject", sa.String(length=64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "limiter",
            "subject",
            "window_start",
            name=op.f("pk_rate_limit_counters"),
        ),
    )
    op.create_index(
        op.f("ix_rate_limit_counters_window_start"),
        "rate_limit_counters",
        ["window_start"],
        unique=False,
    )


def downgrade() -> None:
    """
    remove the schema this revision created
    """
    # Nothing references this table and nothing else references it, so dropping
    # it leaves every earlier revision's data exactly as it was. A build at 0003
    # simply enforces no ingestion rate limit again.
    op.drop_index(op.f("ix_rate_limit_counters_window_start"), table_name="rate_limit_counters")
    op.drop_table("rate_limit_counters")

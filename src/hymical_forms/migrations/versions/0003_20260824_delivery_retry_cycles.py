"""
delivery retry cycles

Manual replay needs a delivery that has already used up its allowance to be able
to earn a fresh one. ``attempts`` cannot do that job on its own: it numbers the
attempt history, so resetting it would make an attempt number repeat, and
leaving it alone would make a replayed delivery fail on its very next attempt.

This adds one column, ``cycle_attempts``, holding the attempts made since the
delivery last entered the queue. The retry policy is measured against it, and a
replay resets it to zero. ``attempts`` keeps counting every request ever made.

Existing deliveries have never been replayed, so their current cycle is the whole
of their history and the backfill sets the new column from ``attempts``. The
downgrade drops the column and nothing else: ``attempts`` still holds every
delivery's lifetime total, which is what the 0002 build reads it as.

Timestamps are not touched here. Nothing in this revision imports application
code, for the reason given in ``0001``: a migration is a frozen record of a
change, not a view of the current models.

revision: 0003
revises: 0002
created: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    add the per-cycle attempt counter a manual replay resets
    """
    # Added nullable so a populated table can take the column at all, then
    # backfilled, then tightened. The tightening is the only step that needs an
    # ALTER, which is why it goes through batch mode: SQLite cannot change a
    # column in place and has to rebuild the table instead. Batch mode is inert
    # on PostgreSQL.
    op.add_column("webhook_deliveries", sa.Column("cycle_attempts", sa.Integer(), nullable=True))
    op.execute("UPDATE webhook_deliveries SET cycle_attempts = attempts")
    with op.batch_alter_table("webhook_deliveries") as batch:
        batch.alter_column("cycle_attempts", existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    """
    remove the per-cycle attempt counter
    """
    # No data is lost that the 0002 build can tell. A delivery replayed while
    # this revision was applied comes back with its lifetime attempt total in
    # ``attempts``, which the older build reads as its retry allowance, so a
    # replayed delivery simply has no allowance left again.
    op.drop_column("webhook_deliveries", "cycle_attempts")

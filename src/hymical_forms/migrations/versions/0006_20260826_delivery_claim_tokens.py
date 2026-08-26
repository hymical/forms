"""
delivery claim tokens

A worker claims a delivery, sends it, and then records what happened. Nothing
made those last two steps agree about who owned the delivery: a worker whose
lease expired mid-request was still holding a row that said ``processing``, and
its completion overwrote the state, the lease and the counters belonging to the
worker that had legitimately reclaimed it.

This adds one nullable column, ``claim_token``, holding the identity of the
claim the row is currently under. A fresh value is written every time the
delivery is claimed and cleared when it stops being claimed, so a completion can
be fenced on the exact claim its request was made under. The lease expiry cannot
do that job: it says when a claim ends, not which claim it is.

Nothing is backfilled. NULL means "no claim is held", which is true of every
delivery that is not ``processing`` and of every row this column is added to.

**Every worker must be on the new build before any of them runs against this
schema.** Applying the migration does not fence a worker that predates it. A
pre-0006 worker neither writes the column when it claims nor consults it when it
completes, so running one alongside a new worker breaks the guarantee in both
directions: its own completion still overwrites a newer worker's state exactly
as before, and because its claim leaves whatever token was already on the row in
place, a superseded new worker can present that stale token, match, and overwrite
the old worker's live claim. That is the deploy order this project already
documents (stop the old processes, migrate, start the new ones), and this
revision is one of the migrations for which it is load-bearing rather than
merely tidy.

The downgrade drops the column. A build without it fences on nothing, which is
the behaviour this revision replaced.

Nothing in this revision imports application code, for the reason given in
``0001``: a migration is a frozen record of a change, not a view of the current
models.

revision: 0006
revises: 0005
created: 2026-08-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    add the column naming the claim a delivery is currently held under
    """
    # Nullable, and stays nullable: the absence of a token is meaningful rather
    # than a gap to be filled, so there is nothing to backfill and no ALTER to
    # tighten afterwards. A plain ADD COLUMN replays under SQLite as well as
    # PostgreSQL, though a fresh SQLite database still cannot reach this revision
    # for the reason recorded in ``0005``.
    op.add_column(
        "webhook_deliveries", sa.Column("claim_token", sa.String(length=36), nullable=True)
    )


def downgrade() -> None:
    """
    remove the claim token column
    """
    # A delivery that is ``processing`` when this runs keeps its lease and is
    # recovered the ordinary way once that lease expires. The older build simply
    # has no fence, which is the weakness ``0006`` exists to close.
    op.drop_column("webhook_deliveries", "claim_token")

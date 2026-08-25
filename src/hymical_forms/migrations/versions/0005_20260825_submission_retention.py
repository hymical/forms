"""
submission retention and self-contained delivery records

Retention has to be able to remove a stored submission without taking the record
of what this service did about it. Until now it could not: a delivery and every
attempt made for it pointed at the submission with a NOT NULL foreign key, so
deleting a submission either failed outright or, with a cascade, would have taken
the operational history with it.

This revision makes a delivery record stand on its own.

``webhook_deliveries.endpoint_id`` is new. The delivery listing used to reach the
endpoint through the submission, which stops working the moment a submission can
be absent. It is snapshotted for the same reason the destination and the signing
secret already are: it describes what the delivery was for, not what the endpoint
happens to be configured as now. Existing rows are backfilled from the submission
they carry, which is exactly the value the join produced.

``webhook_deliveries.submission_id`` and ``delivery_attempts.submission_id``
become nullable, with ``ON DELETE SET NULL`` rather than ``ON DELETE CASCADE``.
That is the whole point: removing a submission unlinks the history from it and
leaves the history itself alone. Only a delivery that has already been delivered
can ever lose its submission, because every state a delivery can still be
attempted from protects the payload it would need.

The submission indexes change to match what submission management reads. The
plain endpoint index is replaced by one that also carries the received timestamp
and the identifier, because a listing is always ordered newest first and the
leftmost column still answers a lookup by endpoint on its own. A second index
over the timestamp and the identifier serves the unfiltered listing and the range
the retention sweep deletes over.

The downgrade removes delivery records whose submission has already been retained
away. The older schema cannot represent a delivery without a submission, so there
is nowhere for those rows to go. Nothing else is touched.

Timestamps are written as ``sa.DateTime(timezone=True)`` rather than the
application's ``UtcDateTime`` decorator, for the reason given in ``0001``: a
migration is a frozen record of a change, not a view of the current models.

This revision alters columns and foreign keys directly rather than through
Alembic's batch mode. Batch mode exists to give SQLite a way to change a column,
by copying the table, and that cannot work here: rebuilding a table other tables
point at fails while SQLite is enforcing foreign keys, which this service turns
on. SQLite is not a migration target. It backs the test suite, whose schema is
built from the models rather than migrated, and it is not a production database
for this service. PostgreSQL performs every operation below in place.

revision: 0005
revises: 0004
created: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    let a delivery record outlive the submission it carried
    """
    # Added nullable so a populated table can take the column at all, then
    # backfilled from the submission, then tightened. Every existing delivery has
    # a submission, because that is what the current schema requires, so the
    # backfill can never leave a row without an endpoint.
    op.add_column(
        "webhook_deliveries", sa.Column("endpoint_id", sa.String(length=64), nullable=True)
    )
    op.execute(
        "UPDATE webhook_deliveries SET endpoint_id = ("
        "SELECT submissions.endpoint_id FROM submissions "
        "WHERE submissions.id = webhook_deliveries.submission_id)"
    )
    op.alter_column(
        "webhook_deliveries", "endpoint_id", existing_type=sa.String(length=64), nullable=False
    )
    op.create_foreign_key(
        "fk_webhook_deliveries_endpoint_id_endpoints",
        "webhook_deliveries",
        "endpoints",
        ["endpoint_id"],
        ["id"],
    )

    # The delete rule is the substance of this revision. SET NULL unlinks the
    # history from the submission; CASCADE would delete it along with the
    # submission, which is the outcome this exists to prevent.
    op.alter_column(
        "webhook_deliveries", "submission_id", existing_type=sa.String(length=36), nullable=True
    )
    op.drop_constraint(
        "fk_webhook_deliveries_submission_id_submissions",
        "webhook_deliveries",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_webhook_deliveries_submission_id_submissions",
        "webhook_deliveries",
        "submissions",
        ["submission_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.alter_column(
        "delivery_attempts", "submission_id", existing_type=sa.String(length=36), nullable=True
    )
    op.drop_constraint(
        "fk_delivery_attempts_submission_id_submissions", "delivery_attempts", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_delivery_attempts_submission_id_submissions",
        "delivery_attempts",
        "submissions",
        ["submission_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_index(
        "ix_webhook_deliveries_endpoint_id_created_at_id",
        "webhook_deliveries",
        ["endpoint_id", "created_at", "id"],
        unique=False,
    )

    op.drop_index(op.f("ix_submissions_endpoint_id"), table_name="submissions")
    op.create_index(
        "ix_submissions_received_at_id", "submissions", ["received_at", "id"], unique=False
    )
    op.create_index(
        "ix_submissions_endpoint_id_received_at_id",
        "submissions",
        ["endpoint_id", "received_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """
    return to a schema where a delivery cannot exist without its submission
    """
    op.drop_index("ix_submissions_endpoint_id_received_at_id", table_name="submissions")
    op.drop_index("ix_submissions_received_at_id", table_name="submissions")
    op.create_index(
        op.f("ix_submissions_endpoint_id"), "submissions", ["endpoint_id"], unique=False
    )
    op.drop_index(
        "ix_webhook_deliveries_endpoint_id_created_at_id", table_name="webhook_deliveries"
    )

    # A delivery whose submission has been retained away cannot be expressed in
    # the schema this returns to, so it is removed along with its attempts. This
    # is the one thing the downgrade destroys, and it destroys nothing an
    # operator still had the submitted content for. Attempts go first, because
    # they point at the deliveries.
    op.execute("DELETE FROM delivery_attempts WHERE submission_id IS NULL")
    op.execute("DELETE FROM webhook_deliveries WHERE submission_id IS NULL")

    op.drop_constraint(
        "fk_delivery_attempts_submission_id_submissions", "delivery_attempts", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_delivery_attempts_submission_id_submissions",
        "delivery_attempts",
        "submissions",
        ["submission_id"],
        ["id"],
    )
    op.alter_column(
        "delivery_attempts", "submission_id", existing_type=sa.String(length=36), nullable=False
    )

    op.drop_constraint(
        "fk_webhook_deliveries_submission_id_submissions",
        "webhook_deliveries",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_webhook_deliveries_submission_id_submissions",
        "webhook_deliveries",
        "submissions",
        ["submission_id"],
        ["id"],
    )
    op.alter_column(
        "webhook_deliveries", "submission_id", existing_type=sa.String(length=36), nullable=False
    )
    op.drop_constraint(
        "fk_webhook_deliveries_endpoint_id_endpoints", "webhook_deliveries", type_="foreignkey"
    )
    op.drop_column("webhook_deliveries", "endpoint_id")

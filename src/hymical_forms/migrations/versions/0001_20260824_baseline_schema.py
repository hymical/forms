"""
baseline schema

The whole schema as it stood at the end of interval 5, when migration history
started. Everything before this point was created with ``create_all`` against
development databases that were never released, so there is nothing earlier to
migrate from and the downgrade simply removes what this created.

Timestamps are written as ``sa.DateTime(timezone=True)`` rather than the
application's ``UtcDateTime`` decorator. The DDL is identical, because that is
exactly what the decorator wraps, and a migration that imports application code
would break the moment that code is refactored. A migration is a frozen record
of a change, not a view of the current models.

revision: 0001
revises:
created: 2026-08-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    create the schema
    """
    op.create_table(
        "endpoints",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("webhook_url", sa.String(length=2048), nullable=True),
        sa.Column("webhook_secret", sa.String(length=70), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_endpoints")),
        # An endpoint has a whole webhook configuration or none of it, so a URL
        # can never exist without the secret its payloads are signed with.
        sa.CheckConstraint(
            "(webhook_url IS NULL) = (webhook_secret IS NULL)",
            name=op.f("ck_endpoints_webhook_configuration"),
        ),
    )

    op.create_table(
        "submissions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("endpoint_id", sa.String(length=64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("payload_fingerprint", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_submissions")),
        sa.ForeignKeyConstraint(
            ["endpoint_id"], ["endpoints.id"], name=op.f("fk_submissions_endpoint_id_endpoints")
        ),
        # What actually enforces idempotency. Both backends treat NULLs in a
        # unique constraint as distinct, so submissions sent without a key stay
        # unrestricted without needing a partial index.
        sa.UniqueConstraint(
            "endpoint_id", "idempotency_key", name="uq_submissions_endpoint_idempotency_key"
        ),
        sa.CheckConstraint(
            "(idempotency_key IS NULL) = (payload_fingerprint IS NULL)",
            name=op.f("ck_submissions_idempotency_identity"),
        ),
    )
    op.create_index(
        op.f("ix_submissions_endpoint_id"), "submissions", ["endpoint_id"], unique=False
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("submission_id", sa.String(length=36), nullable=False),
        sa.Column("destination_url", sa.String(length=2048), nullable=False),
        sa.Column("signing_secret", sa.String(length=70), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_deliveries")),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name=op.f("fk_webhook_deliveries_submission_id_submissions"),
        ),
        # One delivery per submission, which is what makes an idempotent replay
        # unable to queue a second one.
        sa.UniqueConstraint("submission_id", name="uq_webhook_deliveries_submission"),
        # A delivery is finished exactly when it says it is finished.
        sa.CheckConstraint(
            "(state IN ('delivered', 'failed')) = (completed_at IS NOT NULL)",
            name=op.f("ck_webhook_deliveries_completion"),
        ),
    )
    # The column a worker scans on every poll.
    op.create_index(
        op.f("ix_webhook_deliveries_next_attempt_at"),
        "webhook_deliveries",
        ["next_attempt_at"],
        unique=False,
    )

    op.create_table(
        "delivery_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("delivery_id", sa.String(length=36), nullable=False),
        sa.Column("submission_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("destination_url", sa.String(length=2048), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delivery_attempts")),
        sa.ForeignKeyConstraint(
            ["delivery_id"],
            ["webhook_deliveries.id"],
            name=op.f("fk_delivery_attempts_delivery_id_webhook_deliveries"),
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["submissions.id"],
            name=op.f("fk_delivery_attempts_submission_id_submissions"),
        ),
    )
    op.create_index(
        op.f("ix_delivery_attempts_delivery_id"), "delivery_attempts", ["delivery_id"], unique=False
    )
    op.create_index(
        op.f("ix_delivery_attempts_submission_id"),
        "delivery_attempts",
        ["submission_id"],
        unique=False,
    )


def downgrade() -> None:
    """
    remove the schema this revision created
    """
    # Dropped in reverse dependency order so the foreign keys never block a drop.
    op.drop_index(op.f("ix_delivery_attempts_submission_id"), table_name="delivery_attempts")
    op.drop_index(op.f("ix_delivery_attempts_delivery_id"), table_name="delivery_attempts")
    op.drop_table("delivery_attempts")
    op.drop_index(op.f("ix_webhook_deliveries_next_attempt_at"), table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index(op.f("ix_submissions_endpoint_id"), table_name="submissions")
    op.drop_table("submissions")
    op.drop_table("endpoints")

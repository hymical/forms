"""
${message}

revision: ${up_revision}
revises: ${down_revision | comma,n}
created: ${create_date}
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """
    apply this revision
    """
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """
    undo this revision
    """
    ${downgrades if downgrades else "pass"}

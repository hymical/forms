"""
the persisted schema: endpoints and the submissions addressed to them
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, TypeDecorator
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from hymical_forms.ingestion import ENDPOINT_ID_MAX_LENGTH, SUBMISSION_ID_MAX_LENGTH
from hymical_forms.ingestion import Submission as DomainSubmission

ENDPOINT_NAME_MAX_LENGTH = 200


def utcnow() -> datetime:
    """
    read the current time as a timezone-aware UTC timestamp
    :returns: the current instant in UTC
    """
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """
    a timestamp column that always stores and returns timezone-aware UTC values
    """

    # PostgreSQL round-trips ``TIMESTAMPTZ`` faithfully, but SQLite has no
    # timezone-aware storage and hands back a naive datetime. Normalising on the
    # way in and out keeps the two backends indistinguishable to the rest of the
    # code, so a value written as UTC is always read back as UTC.
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """
        convert a value on its way into the database
        :param value: the timestamp being stored, or None
        :param dialect: the active SQLAlchemy dialect
        :returns: the same instant expressed in UTC, or None
        :raises ValueError: if the timestamp carries no timezone
        """
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes cannot be stored, use a timezone-aware value")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        """
        convert a value on its way out of the database
        :param value: the stored timestamp, naive on backends without timezone support
        :param dialect: the active SQLAlchemy dialect
        :returns: a timezone-aware UTC timestamp, or None
        """
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    """
    declarative base for every persisted table
    """


class Endpoint(Base):
    """
    a form ingestion destination that submissions may be addressed to
    """

    # The public endpoint ID is the primary key. It is already unique, immutable
    # in practice (changing it breaks every deployed HTML form pointing at it),
    # and constrained to a short safe character set, so a surrogate key would add
    # a join without buying anything.
    __tablename__ = "endpoints"

    id: Mapped[str] = mapped_column(String(ENDPOINT_ID_MAX_LENGTH), primary_key=True)
    name: Mapped[str] = mapped_column(String(ENDPOINT_NAME_MAX_LENGTH))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Submission(Base):
    """
    a submission that was accepted for a persisted endpoint
    """

    __tablename__ = "submissions"

    id: Mapped[str] = mapped_column(String(SUBMISSION_ID_MAX_LENGTH), primary_key=True)
    endpoint_id: Mapped[str] = mapped_column(
        String(ENDPOINT_ID_MAX_LENGTH),
        ForeignKey("endpoints.id"),
        index=True,
    )
    received_at: Mapped[datetime] = mapped_column(UtcDateTime)

    # Stored as ``{"field name": ["value", ...]}`` under SQLAlchemy's generic JSON
    # type, which is ``json`` on PostgreSQL rather than ``jsonb``. That is
    # deliberate: ``jsonb`` normalises object key order, which would silently
    # reorder a form's fields, and this payload is written once and read whole,
    # so ``jsonb`` indexing would buy nothing here. Every value is a list because
    # JSON has no tuple, so the domain's tuples widen here and narrow again in
    # :meth:`to_domain`.
    fields: Mapped[dict[str, list[str]]] = mapped_column(JSON)

    @classmethod
    def from_domain(cls, submission: DomainSubmission) -> Submission:
        """
        build a persistable row from a validated domain submission
        :param submission: the normalized submission to store
        :returns: an unsaved row mirroring the submission
        """
        return cls(
            id=submission.id,
            endpoint_id=submission.endpoint_id,
            received_at=submission.received_at,
            fields={name: list(values) for name, values in submission.fields.items()},
        )

    def to_domain(self) -> DomainSubmission:
        """
        rebuild the domain submission this row was stored from
        :returns: the submission with its repeated field values restored as tuples
        """
        return DomainSubmission(
            id=self.id,
            endpoint_id=self.endpoint_id,
            received_at=self.received_at,
            fields={name: tuple(values) for name, values in self.fields.items()},
        )

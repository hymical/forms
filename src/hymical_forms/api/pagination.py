"""
the one pagination design the management list routes share

A page is a bounded number of items in a fixed order, newest first, and the
cursor is the opaque identifier of the last item on the page. There is no total
count: counting an operational table on every page is not free, and nothing here
needs the number.

This is deliberately not a framework. It is the two query parameters, the one
error, and the single rule for deciding whether to hand back a cursor, kept in
one place so that both list routes cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Sequence
from http import HTTPStatus
from typing import Annotated

from fastapi import Query

from hymical_forms.errors import ApiError

DEFAULT_PAGE_SIZE = 50

# The ceiling exists so that no request can ask for an unbounded read, whatever
# the caller passes. A limit above it is refused rather than quietly clamped: a
# caller that asked for a thousand rows and silently received a hundred would
# page wrongly and never find out.
MAX_PAGE_SIZE = 100

# Cursors are identifiers this service generated, so this only has to be wide
# enough for the longest of them.
CURSOR_MAX_LENGTH = 64


class InvalidCursor(ApiError):
    """
    raised when a cursor does not name a row that can be continued from
    """

    status_code = HTTPStatus.UNPROCESSABLE_ENTITY
    code = "invalid_cursor"

    def __init__(self) -> None:
        """
        say that the cursor is unusable, without describing what it missed
        """
        super().__init__(
            "The cursor does not continue from a known row. Request the first page "
            "without one to start again.",
            details={"field": "cursor"},
        )


LimitQuery = Annotated[
    int,
    Query(
        ge=1,
        le=MAX_PAGE_SIZE,
        description=f"How many items to return, at most {MAX_PAGE_SIZE}.",
    ),
]

CursorQuery = Annotated[
    str | None,
    Query(
        max_length=CURSOR_MAX_LENGTH,
        description="The `next_cursor` of the previous page. Omit it to read the first page.",
    ),
]


def next_cursor(page: Sequence[str], *, limit: int) -> str | None:
    """
    decide what cursor, if any, the caller should continue from
    :param page: the identifiers of the items being returned, in page order
    :param limit: the page size the request asked for
    :returns: the cursor to continue from, or None when there is certainly no more
    """
    # A full page hands back a cursor even when it happens to be the last one,
    # because knowing that would cost an extra row read on every page. The caller
    # therefore ends on one empty page rather than on a null cursor, which is the
    # ordinary shape of cursor pagination and is documented as such.
    return page[-1] if len(page) == limit else None

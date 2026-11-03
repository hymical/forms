"""
request-boundary checks shared by the management routes

PostgreSQL cannot hold a NUL character in a text value, and the driver refuses
to send one as a parameter. Left to reach the database, a NUL in an identifier,
a filter or a label surfaces as a storage error, which the API reports as an
opaque ``503 storage_unavailable``: a malformed request misreported as an
outage, inviting the client to retry something that can never succeed. These
checks refuse it at the boundary instead, with the answer the same malformed
value would get for any other reason.
"""

from __future__ import annotations

from pydantic import AfterValidator

NUL = "\x00"


def contains_nul(value: str) -> bool:
    """
    report whether a value carries a character no stored value can contain
    :param value: a string taken from the request
    :returns: True if the value contains a NUL character
    """
    return NUL in value


def _refuse_nul(value: str | None) -> str | None:
    """
    let a value through only if it is free of NUL characters
    :param value: the value pydantic has already parsed, or None when it was omitted
    :returns: the value, unchanged
    :raises ValueError: if the value contains a NUL character
    """
    if value is not None and contains_nul(value):
        # The value itself is not echoed: it is reported by field name only, as
        # every other validation failure is.
        raise ValueError("must not contain a NUL character")
    return value


# Attach to a query parameter or a body field with ``Annotated[..., NoNul]``. A
# refusal is an ordinary ``422 invalid_request`` naming the field.
NoNul = AfterValidator(_refuse_nul)

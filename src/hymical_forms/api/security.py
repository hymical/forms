"""
the management authentication boundary

One dependency resolves a management API key, and every route that administers
the service declares it. A future management route gets these rules by asking
for the same dependency rather than by reading the ``Authorization`` header
itself, which is what stops the rules from drifting apart route by route.

Public routes deliberately do not appear here. Form ingestion and the health
check are reachable without a credential, because an ingestion URL is meant to
sit in the ``action`` attribute of somebody's HTML form.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from http import HTTPStatus
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from hymical_forms import apikeys, storage
from hymical_forms.db import SessionDep
from hymical_forms.errors import ApiError
from hymical_forms.models import utcnow

logger = logging.getLogger(__name__)

# RFC 9110 says a 401 carries a challenge, and a client library that looks for
# one to decide how to authenticate should find it.
WWW_AUTHENTICATE = {"WWW-Authenticate": "Bearer"}


class AuthenticationRequired(ApiError):
    """
    raised when a management request arrived without a usable bearer credential
    """

    status_code = HTTPStatus.UNAUTHORIZED
    code = "authentication_required"
    headers = WWW_AUTHENTICATE

    def __init__(self) -> None:
        """
        state how a management request is meant to authenticate
        """
        # Separate from ``invalid_api_key`` because the two are genuinely
        # different problems for an integrator to fix, and because whether a
        # request carried an Authorization header is something its sender
        # already knows. Nothing is disclosed by saying so.
        super().__init__(
            f"This endpoint requires a management API key. {apikeys.MANAGEMENT_KEY_RULE}"
        )


class InvalidApiKey(ApiError):
    """
    raised when a bearer credential was supplied but does not authenticate
    """

    # 401 rather than 403. A 403 says "I know who you are and you may not do
    # this", which needs a permission model, and this build has none: every valid
    # management key may do everything a management key can do.
    status_code = HTTPStatus.UNAUTHORIZED
    code = "invalid_api_key"
    headers = WWW_AUTHENTICATE

    def __init__(self) -> None:
        """
        refuse a credential without describing what was wrong with it
        """
        # Malformed, unknown and revoked all arrive here and all leave with these
        # exact words. Telling them apart would let somebody sort guesses into
        # "nearly right" and "wrong", which is the signal enumeration runs on.
        # The supplied credential is never echoed.
        super().__init__("The management API key is not valid.")


@dataclass(frozen=True, slots=True)
class ManagementPrincipal:
    """
    the identity a management request authenticated as
    """

    # Only non-secret identity crosses this boundary. Routes are handed no
    # credential material, so no handler downstream is in a position to log it,
    # store it, or forward it to somebody else's server.
    key_id: str
    name: str
    display_prefix: str


_bearer = HTTPBearer(
    scheme_name="ManagementApiKey",
    description="A management API key, created with `python -m hymical_forms.cli create-key`.",
    # Errors are raised by this module so that they reach the shared JSON
    # envelope. Left on, the scheme would answer with FastAPI's own error body
    # and a caller would meet two different error shapes from one API.
    auto_error=False,
)


def require_management_key(
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> ManagementPrincipal:
    """
    resolve the management key a request authenticated with
    :param session: the session this request does its database work through
    :param credentials: the parsed bearer credential, or None if there was no usable one
    :returns: the non-secret identity of the key that authenticated
    :raises AuthenticationRequired: if no bearer credential was supplied
    :raises InvalidApiKey: if the credential is malformed, unknown or revoked
    """
    if credentials is None:
        # No Authorization header, an unparseable one, a scheme other than
        # Bearer, or Bearer with nothing after it.
        raise AuthenticationRequired()

    supplied = credentials.credentials
    if not apikeys.is_valid_management_key(supplied):
        # A syntax gate in front of the database, so a sweep of unrelated tokens
        # costs no query. It is not a security decision: a well-formed key that
        # is not in the table is refused in exactly the same words.
        raise InvalidApiKey()

    digest = apikeys.digest_key(supplied)
    record = storage.find_management_key_by_digest(session, digest)
    if record is None or not apikeys.digests_match(digest, record.key_digest):
        raise InvalidApiKey()
    if record.revoked_at is not None:
        # Read from the row on every request. Nothing caches a key anywhere, so a
        # revocation takes effect on the next request rather than whenever some
        # cache would have expired.
        raise InvalidApiKey()

    _record_use(session, record.id)
    return ManagementPrincipal(
        key_id=record.id,
        name=record.name,
        display_prefix=record.display_prefix,
    )


ManagementKeyDep = Annotated[ManagementPrincipal, Depends(require_management_key)]


def _record_use(session: Session, key_id: str) -> None:
    """
    note that a key authenticated a request, without letting that failure matter
    :param session: the session to write through
    :param key_id: the key that authenticated
    """
    # This is telemetry, not authentication, and it is written after the decision
    # has already been made. One small write per authenticated management request
    # is affordable because management traffic is rare by nature; it would not be
    # if this ran on the ingestion path, which is exactly why it does not.
    #
    # The failure is swallowed on purpose. A database that cannot record a
    # timestamp must not be able to turn a valid credential into a 401, and the
    # rollback is what hands the route a usable session afterwards.
    try:
        storage.record_management_key_use(session, key_id, now=utcnow())
    except SQLAlchemyError:
        session.rollback()
        logger.warning("could not record last use of management key %s", key_id)

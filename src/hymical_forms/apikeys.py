"""
management API key rules: format, generation and digesting

Nothing in this module performs I/O or knows about HTTP or the database.
Minting a credential is kept apart from storing one so that the format, the
entropy and the digest can be tested without a database, and so that the only
moment a full key exists in this process is the moment it was asked for.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from dataclasses import dataclass

# A recognisable prefix, so an operator who finds this string in a config file
# knows immediately whose credential it is and what to revoke. ``live`` leaves
# room for a differently scoped credential later without changing what this one
# means.
MANAGEMENT_KEY_PREFIX = "hym_live_"

# 32 random bytes from the OS, rendered as unpadded base64url: 256 bits of
# entropy in 43 characters. Deliberately not a UUID4, which carries 122 bits and
# spends 6 of them on fixed version and variant nibbles. This credential sits on
# the public internet and can be guessed at indefinitely, so it is sized for
# that rather than for being a database identifier.
MANAGEMENT_KEY_SECRET_BYTES = 32
MANAGEMENT_KEY_SECRET_LENGTH = 43

MANAGEMENT_KEY_LENGTH = len(MANAGEMENT_KEY_PREFIX) + MANAGEMENT_KEY_SECRET_LENGTH

_SECRET_PATTERN = re.compile(rf"[A-Za-z0-9_-]{{{MANAGEMENT_KEY_SECRET_LENGTH}}}")

MANAGEMENT_KEY_ID_PREFIX = "mk_"
MANAGEMENT_KEY_ID_MAX_LENGTH = len(MANAGEMENT_KEY_ID_PREFIX) + 32

MANAGEMENT_KEY_NAME_MAX_LENGTH = 200

# The prefix plus the first few characters of the secret, kept so that a key can
# be recognised in a listing without the listing holding the credential. Eight of
# forty-three base64url characters leaves 210 bits unknown, which is still far
# more than anything else in this service relies on.
DISPLAY_PREFIX_SECRET_CHARS = 8
DISPLAY_PREFIX_LENGTH = len(MANAGEMENT_KEY_PREFIX) + DISPLAY_PREFIX_SECRET_CHARS

# A SHA-256 digest rendered as hex.
KEY_DIGEST_LENGTH = 64

# Stated once so that every message mentioning the rule words it identically.
MANAGEMENT_KEY_RULE = (
    f"Send a management API key as 'Authorization: Bearer {MANAGEMENT_KEY_PREFIX}...'."
)


@dataclass(frozen=True, slots=True)
class GeneratedKey:
    """
    a freshly minted management API key, in both of its forms
    """

    # ``key`` is the credential and the only copy of it that will ever exist: it
    # is handed to the operator once and is not among the fields the database is
    # given. The other three are what may safely be persisted.
    id: str
    key: str
    display_prefix: str
    digest: str


def new_management_key() -> GeneratedKey:
    """
    mint a management API key and the safe representation of it
    :returns: the full credential together with the identity and digest to store
    """
    secret = secrets.token_urlsafe(MANAGEMENT_KEY_SECRET_BYTES)
    key = f"{MANAGEMENT_KEY_PREFIX}{secret}"
    return GeneratedKey(
        id=new_management_key_id(),
        key=key,
        display_prefix=display_prefix(key),
        digest=digest_key(key),
    )


def new_management_key_id() -> str:
    """
    generate an opaque, non-secret identifier for a management key
    :returns: a fresh key id such as ``mk_1f0c9a...``
    """
    # Separate from the credential on purpose. This is what an operator names in
    # a revoke command and what a log line may safely carry, and knowing it tells
    # nobody anything about the secret it belongs to.
    return f"{MANAGEMENT_KEY_ID_PREFIX}{uuid.uuid4().hex}"


def digest_key(key: str) -> str:
    """
    reduce a full management API key to the value that may be stored
    :param key: the full credential as the client would send it
    :returns: a hex SHA-256 digest of the credential
    """
    # A single SHA-256, not a password hash. Password hashing is slow on purpose
    # because passwords are low-entropy and guessable; this secret is 256 bits
    # from the OS random source, so an attacker holding the digest has nothing to
    # guess at and the cost would buy only latency on every management request.
    #
    # No pepper either. A pepper protects a digest that is feasible to attack
    # offline, which this one is not, and it would introduce a second secret that
    # has to be deployed, backed up and rotated, whose loss would silently
    # invalidate every key in the table.
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def digests_match(supplied: str, stored: str) -> bool:
    """
    compare a computed digest against a stored one without leaking timing
    :param supplied: digest of the credential the client sent
    :param stored: digest read from the database
    :returns: True if the two digests are identical
    """
    # Candidates are found by digest, so the database has already compared these
    # for us and this is belt and braces. It is here because it is the primitive
    # a credential comparison should be written with: if lookup ever changes to
    # resolve a candidate by its display prefix, this line is what keeps the
    # comparison that decides the answer safe.
    return hmac.compare_digest(supplied, stored)


def is_valid_management_key(key: str) -> bool:
    """
    report whether a supplied credential is even shaped like one of ours
    :param key: the bearer credential taken from the Authorization header
    :returns: True if the credential has this service's prefix and secret shape
    """
    # A cheap syntax gate in front of the database, so an internet-wide sweep of
    # unrelated tokens costs no query. It is not a security check: a well-formed
    # key that is not in the table is refused in exactly the same words.
    if not key.startswith(MANAGEMENT_KEY_PREFIX):
        return False
    return _SECRET_PATTERN.fullmatch(key[len(MANAGEMENT_KEY_PREFIX) :]) is not None


def display_prefix(key: str) -> str:
    """
    take the non-secret fragment of a key that identifies it in a listing
    :param key: the full credential
    :returns: the prefix plus the first few characters of the secret
    """
    return key[:DISPLAY_PREFIX_LENGTH]

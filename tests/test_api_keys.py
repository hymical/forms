"""
management API key generation, digesting and storage
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import DEFAULT_KEY_NAME, issue_management_key, management_key, open_session
from hymical_forms import apikeys, models, storage
from hymical_forms.models import utcnow

# --- format and entropy ------------------------------------------------------


def test_a_generated_key_is_recognisably_ours(empty_client: TestClient) -> None:
    key = issue_management_key(empty_client)

    assert key.startswith(apikeys.MANAGEMENT_KEY_PREFIX)
    assert len(key) == apikeys.MANAGEMENT_KEY_LENGTH
    assert apikeys.is_valid_management_key(key)


def test_two_generated_keys_differ() -> None:
    """
    the secret has to come from a random source, not from anything derivable
    """
    # Twenty is plenty to catch a constant or a counter, and asserts nothing about
    # any particular random value.
    keys = {apikeys.new_management_key().key for _ in range(20)}
    ids = {apikeys.new_management_key().id for _ in range(20)}

    assert len(keys) == 20
    assert len(ids) == 20


def test_the_key_id_is_not_derived_from_the_secret() -> None:
    generated = apikeys.new_management_key()

    assert generated.id.startswith(apikeys.MANAGEMENT_KEY_ID_PREFIX)
    assert generated.id not in generated.key
    assert generated.key.removeprefix(apikeys.MANAGEMENT_KEY_PREFIX) not in generated.id


def test_the_display_prefix_reveals_only_its_first_characters() -> None:
    generated = apikeys.new_management_key()

    assert generated.key.startswith(generated.display_prefix)
    assert len(generated.display_prefix) == apikeys.DISPLAY_PREFIX_LENGTH
    assert len(generated.display_prefix) < len(generated.key)


def test_the_digest_is_a_hex_sha256_of_the_whole_key() -> None:
    generated = apikeys.new_management_key()

    assert len(generated.digest) == apikeys.KEY_DIGEST_LENGTH
    assert generated.digest == apikeys.digest_key(generated.key)
    assert generated.digest != apikeys.digest_key(generated.key + "x")


@pytest.mark.parametrize(
    ("description", "candidate"),
    [
        ("empty", ""),
        ("the prefix alone", apikeys.MANAGEMENT_KEY_PREFIX),
        ("no prefix", "a" * apikeys.MANAGEMENT_KEY_SECRET_LENGTH),
        ("the wrong prefix", "hym_test_" + "a" * apikeys.MANAGEMENT_KEY_SECRET_LENGTH),
        ("too short", apikeys.MANAGEMENT_KEY_PREFIX + "a" * 10),
        ("too long", apikeys.MANAGEMENT_KEY_PREFIX + "a" * 100),
        ("a character outside base64url", apikeys.MANAGEMENT_KEY_PREFIX + "a!" + "a" * 41),
        ("a uuid", "hym_live_f81d4fae-7dec-11d0-a765-00a0c91e6bf6"),
    ],
)
def test_a_malformed_key_is_not_even_well_formed(description: str, candidate: str) -> None:
    assert not apikeys.is_valid_management_key(candidate), description


# --- what the database is allowed to hold ------------------------------------


def test_the_stored_row_holds_no_credential(empty_client: TestClient) -> None:
    """
    the whole point of the digest is that the table is not a set of working keys
    :param empty_client: test client whose app holds no endpoints
    """
    key = management_key(empty_client)
    secret = key.removeprefix(apikeys.MANAGEMENT_KEY_PREFIX)

    with open_session(empty_client) as session:
        rows = list(session.scalars(select(models.ManagementApiKey)))

    assert len(rows) == 1
    stored = rows[0]
    values = [str(getattr(stored, column.name)) for column in models.ManagementApiKey.__table__.c]
    assert key not in values
    assert not any(secret in value for value in values)
    assert stored.key_digest == apikeys.digest_key(key)


def test_the_stored_digest_authenticates_the_generated_key(empty_client: TestClient) -> None:
    key = issue_management_key(empty_client)

    with open_session(empty_client) as session:
        found = storage.find_management_key_by_digest(session, apikeys.digest_key(key))

    assert found is not None
    assert found.is_active


def test_another_key_does_not_resolve_to_a_stored_one(empty_client: TestClient) -> None:
    issue_management_key(empty_client)
    other = apikeys.new_management_key()

    with open_session(empty_client) as session:
        assert storage.find_management_key_by_digest(session, other.digest) is None


def test_revoking_records_the_moment_without_deleting_the_row(empty_client: TestClient) -> None:
    key = issue_management_key(empty_client)

    with open_session(empty_client) as session:
        stored = storage.find_management_key_by_digest(session, apikeys.digest_key(key))
        assert stored is not None
        revoked = storage.revoke_management_key(session, stored.id, now=utcnow())

    assert revoked is not None
    assert revoked.revoked_at is not None
    assert not revoked.is_active

    with open_session(empty_client) as session:
        assert storage.get_management_key(session, revoked.id) is not None


def test_revoking_twice_keeps_the_first_moment(empty_client: TestClient) -> None:
    """
    revocation is idempotent, and must not move when the credential stopped working
    :param empty_client: test client whose app holds no endpoints
    """
    key = issue_management_key(empty_client)

    with open_session(empty_client) as session:
        stored = storage.find_management_key_by_digest(session, apikeys.digest_key(key))
        assert stored is not None
        first = storage.revoke_management_key(session, stored.id, now=utcnow())
        assert first is not None
        moment = first.revoked_at

        second = storage.revoke_management_key(session, stored.id, now=utcnow())

    assert second is not None
    assert second.revoked_at == moment


def test_revoking_an_unknown_key_reports_it(empty_client: TestClient) -> None:
    with open_session(empty_client) as session:
        assert storage.revoke_management_key(session, "mk_nope", now=utcnow()) is None


def test_listing_returns_every_key(empty_client: TestClient) -> None:
    issue_management_key(empty_client, name="second-operator")

    with open_session(empty_client) as session:
        names = [key.name for key in storage.list_management_keys(session)]

    # The fixture's own key, plus the one this test added.
    assert set(names) == {DEFAULT_KEY_NAME, "second-operator"}

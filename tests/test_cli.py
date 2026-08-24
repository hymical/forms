"""
the operator command line: creating, listing and revoking management keys

Each test runs against a SQLite file rather than the in-memory database the API
tests use, because the CLI is a separate process in real life and opens its own
connection to whatever ``FORMS_DATABASE_URL`` names.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from conftest import IsolatedSettings, bearer, build_settings
from hymical_forms import apikeys, cli, models
from hymical_forms.app import create_app
from hymical_forms.db import create_engine_from_url, create_session_factory
from hymical_forms.schema import create_all

CREATE_BODY = {"id": "contact-form", "name": "Contact form"}


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """
    provide a migrated SQLite file the CLI will find through the environment
    :param tmp_path: pytest fixture giving this test a directory of its own
    :param monkeypatch: pytest fixture used to point the CLI at that database
    :returns: an iterator yielding the database URL
    """
    url = f"sqlite:///{tmp_path / 'forms.db'}"
    engine = create_engine_from_url(url)
    create_all(engine)
    engine.dispose()

    monkeypatch.setenv("FORMS_DATABASE_URL", url)
    # The CLI builds Settings itself, which would otherwise read a .env file the
    # developer running the suite happens to have. This is the same isolation the
    # API tests get from building their settings explicitly.
    monkeypatch.setattr(cli, "Settings", IsolatedSettings)
    yield url


@pytest.fixture
def unmigrated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """
    provide a database URL naming a file that holds no schema at all
    :param tmp_path: pytest fixture giving this test a directory of its own
    :param monkeypatch: pytest fixture used to point the CLI at that database
    :returns: an iterator yielding the database URL
    """
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    monkeypatch.setenv("FORMS_DATABASE_URL", url)
    monkeypatch.setattr(cli, "Settings", IsolatedSettings)
    yield url


def stored_keys(url: str) -> list[models.ManagementApiKey]:
    """
    read every management key row straight out of the database
    :param url: the database to read from
    :returns: the stored keys
    """
    engine = create_engine_from_url(url)
    try:
        with create_session_factory(engine)() as session:
            return list(session.scalars(select(models.ManagementApiKey)))
    finally:
        engine.dispose()


def created_key(output: str) -> str:
    """
    pick the credential out of what create-key printed
    :param output: everything the command wrote to stdout
    :returns: the full key
    """
    keys = [word for word in output.split() if word.startswith(apikeys.MANAGEMENT_KEY_PREFIX)]
    assert len(keys) == 1, f"expected exactly one credential in the output, found {len(keys)}"
    return keys[0]


# --- create-key --------------------------------------------------------------


def test_create_key_prints_the_credential_once(
    database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["create-key", "--name", "local-admin"]) == 0

    output = capsys.readouterr().out
    key = created_key(output)
    assert apikeys.is_valid_management_key(key)
    assert output.count(key) == 1


def test_create_key_tells_the_operator_to_save_it(
    database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["create-key", "--name", "local-admin"])

    output = capsys.readouterr().out
    assert "Save this key now" in output
    assert "local-admin" in output


def test_create_key_stores_no_plaintext_credential(
    database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    a copy of the table must not be a set of working credentials
    :param database: a migrated database the CLI is pointed at
    :param capsys: pytest fixture capturing what the command printed
    """
    cli.main(["create-key", "--name", "local-admin"])
    key = created_key(capsys.readouterr().out)
    secret = key.removeprefix(apikeys.MANAGEMENT_KEY_PREFIX)

    rows = stored_keys(database)

    assert len(rows) == 1
    values = [str(getattr(rows[0], column.name)) for column in models.ManagementApiKey.__table__.c]
    assert not any(secret in value for value in values)
    assert rows[0].key_digest == apikeys.digest_key(key)
    assert rows[0].name == "local-admin"


def test_a_key_created_by_the_cli_authenticates(
    database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    the CLI and the HTTP boundary have to agree on what a credential is
    :param database: a migrated database the CLI is pointed at
    :param capsys: pytest fixture capturing what the command printed
    """
    cli.main(["create-key", "--name", "local-admin"])
    key = created_key(capsys.readouterr().out)

    app = create_app(build_settings(database_url=database))
    with TestClient(app) as client:
        response = client.post("/endpoints", json=CREATE_BODY, headers=bearer(key))

    assert response.status_code == 201


def test_two_created_keys_differ(database: str, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["create-key", "--name", "first"])
    first = created_key(capsys.readouterr().out)
    cli.main(["create-key", "--name", "second"])
    second = created_key(capsys.readouterr().out)

    assert first != second
    assert len(stored_keys(database)) == 2


# --- list-keys ---------------------------------------------------------------


def test_list_keys_says_so_when_there_are_none(
    database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["list-keys"]) == 0

    assert "No management API keys exist" in capsys.readouterr().out


def test_list_keys_shows_safe_metadata_only(
    database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["create-key", "--name", "local-admin"])
    key = created_key(capsys.readouterr().out)

    assert cli.main(["list-keys"]) == 0

    output = capsys.readouterr().out
    assert key not in output
    assert key.removeprefix(apikeys.MANAGEMENT_KEY_PREFIX) not in output
    assert stored_keys(database)[0].id in output
    assert "local-admin" in output
    assert apikeys.display_prefix(key) in output
    assert "active" in output
    assert "never" in output


def test_list_keys_reports_a_revoked_key_as_revoked(
    database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["create-key", "--name", "local-admin"])
    capsys.readouterr()
    key_id = stored_keys(database)[0].id
    cli.main(["revoke-key", key_id])
    capsys.readouterr()

    cli.main(["list-keys"])

    assert "revoked" in capsys.readouterr().out


# --- revoke-key --------------------------------------------------------------


def test_revoke_key_withdraws_the_credential(
    database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["create-key", "--name", "local-admin"])
    key = created_key(capsys.readouterr().out)
    key_id = stored_keys(database)[0].id

    assert cli.main(["revoke-key", key_id]) == 0

    output = capsys.readouterr().out
    assert "Revoked management API key" in output
    assert key not in output
    assert stored_keys(database)[0].revoked_at is not None


def test_a_revoked_key_no_longer_authenticates(
    database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["create-key", "--name", "local-admin"])
    key = created_key(capsys.readouterr().out)
    cli.main(["revoke-key", stored_keys(database)[0].id])

    app = create_app(build_settings(database_url=database))
    with TestClient(app) as client:
        response = client.post("/endpoints", json=CREATE_BODY, headers=bearer(key))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_revoking_twice_is_accepted_and_says_so(
    database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["create-key", "--name", "local-admin"])
    capsys.readouterr()
    key_id = stored_keys(database)[0].id
    cli.main(["revoke-key", key_id])
    first = stored_keys(database)[0].revoked_at

    assert cli.main(["revoke-key", key_id]) == 0

    assert "already revoked" in capsys.readouterr().out
    assert stored_keys(database)[0].revoked_at == first


def test_revoking_an_unknown_key_fails_cleanly(
    database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["revoke-key", "mk_does_not_exist"]) == 1

    captured = capsys.readouterr()
    assert "No management API key" in captured.err
    assert captured.out == ""


# --- operator errors ---------------------------------------------------------


def test_an_unmigrated_database_produces_a_useful_error(
    unmigrated_database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """
    the CLI makes the same schema check the API and the worker make on startup
    :param unmigrated_database: a database URL naming a file with no schema
    :param capsys: pytest fixture capturing what the command printed
    """
    assert cli.main(["create-key", "--name", "local-admin"]) == 1

    captured = capsys.readouterr()
    assert "schema is not ready" in captured.err
    assert "alembic upgrade head" in captured.err
    assert captured.out == ""


def test_a_missing_database_url_produces_a_useful_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "Settings", IsolatedSettings)

    assert cli.main(["create-key", "--name", "local-admin"]) == 1

    assert "FORMS_DATABASE_URL is not set" in capsys.readouterr().err


def test_a_database_that_cannot_be_opened_produces_a_useful_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FORMS_DATABASE_URL", f"sqlite:///{tmp_path / 'missing' / 'forms.db'}")
    monkeypatch.setattr(cli, "Settings", IsolatedSettings)

    assert cli.main(["create-key", "--name", "local-admin"]) == 1

    captured = capsys.readouterr()
    assert "The database could not be used" in captured.err
    assert captured.out == ""


def test_a_driver_message_never_carries_the_database_password() -> None:
    """
    some driver errors repeat the URL back, and the password must not ride along
    """
    url = "postgresql+psycopg://forms:sup3rs3cret@localhost:5432/forms"

    redacted = cli._redact(f"could not connect using {url}", url)

    assert "sup3rs3cret" not in redacted
    assert "***" in redacted


def test_an_unparseable_database_url_is_not_repeated_back() -> None:
    redacted = cli._redact("Could not parse SQLAlchemy URL from 'sup3rs3cret'", "sup3rs3cret")

    assert "sup3rs3cret" not in redacted


def test_a_failing_command_creates_no_key(
    unmigrated_database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["create-key", "--name", "local-admin"])

    assert apikeys.MANAGEMENT_KEY_PREFIX not in capsys.readouterr().out

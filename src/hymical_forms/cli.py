"""
the operator command line for management API keys

Run it as its own process, against the database ``FORMS_DATABASE_URL`` names::

    python -m hymical_forms.cli create-key --name local-admin
    python -m hymical_forms.cli list-keys
    python -m hymical_forms.cli revoke-key mk_1f0c9a...

Keys are minted here rather than over HTTP, and that is the whole answer to how
the first one comes into being. A route that issued a management credential
without needing one would be an unauthenticated way to gain full management
access, which is the thing this boundary exists to remove. Nothing is generated
at startup and no key is shipped in this repository, so a deployment has exactly
the credentials an operator deliberately created.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from typing import TextIO

from pydantic import ValidationError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import Session

from hymical_forms import apikeys, storage
from hymical_forms.config import Settings
from hymical_forms.db import create_engine_from_url, create_session_factory
from hymical_forms.models import ManagementApiKey, utcnow
from hymical_forms.schema import SchemaNotReady, verify_schema

PROGRAM = "python -m hymical_forms.cli"

EXIT_OK = 0
EXIT_FAILED = 1


def main(argv: Sequence[str] | None = None) -> int:
    """
    run one operator command
    :param argv: command line arguments, or None to read them from the process
    :returns: the process exit code
    """
    parser = build_parser()
    arguments = parser.parse_args(argv)

    try:
        settings = Settings()
    except ValidationError:
        # The message is written here rather than relayed, because pydantic's own
        # is a validation report about a settings model the operator has never
        # seen and does not need to learn about.
        print(
            "FORMS_DATABASE_URL is not set. Export it, or put it in a .env file "
            "in this directory, and run the command again.",
            file=sys.stderr,
        )
        return EXIT_FAILED

    try:
        return _run(arguments, settings, out=sys.stdout)
    except SchemaNotReady as exc:
        print(f"The database schema is not ready: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except SQLAlchemyError as exc:
        # Driver text is useful to whoever is debugging a connection, but it can
        # repeat back the URL it was handed, so the password is taken out of it
        # first. No API key can appear here: none is ever passed to the database.
        print(
            f"The database could not be used: {_redact(str(exc), settings.database_url)}",
            file=sys.stderr,
        )
        return EXIT_FAILED


def build_parser() -> argparse.ArgumentParser:
    """
    build the argument parser for the operator commands
    :returns: a parser covering every subcommand
    """
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Create, list and revoke Hymical Forms management API keys.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    create = subcommands.add_parser(
        "create-key",
        help="Create a management API key and print it once.",
    )
    create.add_argument(
        "--name",
        required=True,
        help="Human-readable label, so this key can be recognised later.",
    )

    subcommands.add_parser(
        "list-keys",
        help="List management keys and their state. Never shows a credential.",
    )

    revoke = subcommands.add_parser(
        "revoke-key",
        help="Revoke a management API key by its key ID.",
    )
    revoke.add_argument("key_id", help="The key ID, as shown by list-keys.")

    return parser


def _run(arguments: argparse.Namespace, settings: Settings, *, out: TextIO) -> int:
    """
    open the database, check the schema, and dispatch to the chosen command
    :param arguments: the parsed command line
    :param settings: the configuration naming the database to work against
    :param out: the stream ordinary output is written to
    :returns: the process exit code
    """
    engine = create_engine_from_url(settings.database_url)
    try:
        # The same check the API and the worker make on startup. Writing a key
        # into a database at an older revision would either fail on a missing
        # table or, worse, appear to work against a schema this build does not
        # understand.
        verify_schema(engine)
        with create_session_factory(engine)() as session:
            return _dispatch(arguments, session, out=out)
    finally:
        engine.dispose()


def _dispatch(arguments: argparse.Namespace, session: Session, *, out: TextIO) -> int:
    """
    run the chosen command against an open session
    :param arguments: the parsed command line
    :param session: the session to do the work through
    :param out: the stream ordinary output is written to
    :returns: the process exit code
    """
    if arguments.command == "create-key":
        return _create_key(session, name=arguments.name, out=out)
    if arguments.command == "list-keys":
        return _list_keys(session, out=out)
    return _revoke_key(session, key_id=arguments.key_id, out=out)


def _create_key(session: Session, *, name: str, out: TextIO) -> int:
    """
    mint a management key, store its safe representation, and show it once
    :param session: the session to write through
    :param name: human-readable label for the key
    :param out: the stream ordinary output is written to
    :returns: the process exit code
    """
    generated = apikeys.new_management_key()
    storage.create_management_key(
        session,
        key_id=generated.id,
        name=name,
        display_prefix=generated.display_prefix,
        key_digest=generated.digest,
        now=utcnow(),
    )
    session.commit()

    # Printed after the commit, so a key that could not be stored is never
    # presented as one that works. The credential goes to stdout and nowhere
    # else: it is not logged, and nothing writes it to disk.
    print(f"Created management API key {generated.id} ({name}).", file=out)
    print(file=out)
    print(f"    {generated.key}", file=out)
    print(file=out)
    print(
        "Save this key now. It is shown here and nowhere else: the server stores\n"
        "only a digest of it and cannot show it again. If you lose it, create a\n"
        "replacement and revoke this one by its key ID.",
        file=out,
    )
    return EXIT_OK


def _list_keys(session: Session, *, out: TextIO) -> int:
    """
    show every management key without showing any credential
    :param session: the session to query through
    :param out: the stream ordinary output is written to
    :returns: the process exit code
    """
    keys = storage.list_management_keys(session)
    if not keys:
        print("No management API keys exist. Create one with 'create-key --name ...'.", file=out)
        return EXIT_OK

    # Only columns the database actually holds, and the database holds no
    # credential, so there is nothing here that could reconstruct one.
    print(_row("KEY ID", "NAME", "PREFIX", "CREATED", "LAST USED", "STATUS"), file=out)
    for key in keys:
        print(
            _row(
                key.id,
                key.name,
                key.display_prefix,
                _moment(key.created_at),
                _moment(key.last_used_at),
                "active" if key.is_active else f"revoked {_moment(key.revoked_at)}",
            ),
            file=out,
        )
    return EXIT_OK


def _revoke_key(session: Session, *, key_id: str, out: TextIO) -> int:
    """
    withdraw a management key so that it stops authenticating
    :param session: the session to write through
    :param key_id: the identifier of the key to revoke
    :param out: the stream ordinary output is written to
    :returns: the process exit code
    """
    already_revoked = _revocation_moment(session, key_id)
    key = storage.revoke_management_key(session, key_id, now=utcnow())
    if key is None:
        print(f"No management API key with the ID {key_id!r} exists.", file=sys.stderr)
        return EXIT_FAILED

    if already_revoked is not None:
        # Idempotent on purpose, and it reports the moment the credential
        # actually stopped working rather than the moment of this second attempt.
        print(
            f"Management API key {key.id} ({key.name}) was already revoked at "
            f"{_moment(already_revoked)}.",
            file=out,
        )
        return EXIT_OK

    print(
        f"Revoked management API key {key.id} ({key.name}). It will not authenticate "
        "another request.",
        file=out,
    )
    return EXIT_OK


def _revocation_moment(session: Session, key_id: str) -> datetime | None:
    """
    read when a key was revoked, before this command possibly revokes it
    :param session: the session to query through
    :param key_id: the identifier of the key to inspect
    :returns: when it was revoked, or None if it exists and is active or does not exist
    """
    key: ManagementApiKey | None = storage.get_management_key(session, key_id)
    return key.revoked_at if key is not None else None


def _row(*columns: str) -> str:
    """
    lay one listing row out in fixed-width columns
    :param columns: the cell values, in listing order
    :returns: the rendered line
    """
    widths = (35, 24, 17, 26, 26, 0)
    return "  ".join(value.ljust(width) for value, width in zip(columns, widths, strict=True))


def _moment(value: datetime | None) -> str:
    """
    render a timestamp for a listing
    :param value: the instant to render, or None if there is not one
    :returns: an ISO 8601 timestamp, or ``never`` when there is none
    """
    return value.isoformat(timespec="seconds") if value is not None else "never"


def _redact(text: str, database_url: str) -> str:
    """
    hide the database password if a driver message repeated it back
    :param text: the message about to be shown to the operator
    :param database_url: the URL this invocation was configured with
    :returns: the message with any occurrence of the password replaced
    """
    try:
        password = make_url(database_url).password
    except ArgumentError:
        # The URL did not parse, which is very likely what the message is about.
        # Nothing can be located in it to redact, so nothing is shown.
        return "the configured FORMS_DATABASE_URL is not a usable SQLAlchemy URL"
    return text.replace(password, "***") if password else text


if __name__ == "__main__":
    raise SystemExit(main())

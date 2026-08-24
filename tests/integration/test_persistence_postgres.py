"""
representative application flows against a migrated PostgreSQL database

Not a second copy of the API suite. The point is that the whole path works on
the schema Alembic produced, on the database this service is actually meant to
run on.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from hymical_forms import models
from hymical_forms.webhooks import DeliveryState

ENDPOINT = "/f/contact-form"
KEY = "b8f1c2d4e5a67890b8f1c2d4e5a67890"


def create_endpoint(client: TestClient, *, webhook: bool = True) -> dict[str, str]:
    """
    register an endpoint through the API
    :param client: the client to register through
    :param webhook: whether to configure a webhook destination
    :returns: the created endpoint as the API returned it
    """
    body: dict[str, object] = {"id": "contact-form", "name": "Contact form"}
    if webhook:
        body["webhook_url"] = "https://example.invalid/hook"
    response = client.post("/endpoints", json=body)
    assert response.status_code == 201, response.text
    return dict(response.json())


def test_the_whole_ingestion_flow_works_on_postgresql(
    pg_client: TestClient, sessions: sessionmaker[Session]
) -> None:
    """
    endpoint, submission and queued delivery all land on the migrated schema
    :param pg_client: an API client backed by PostgreSQL
    :param sessions: factory handing out independent connections
    """
    endpoint = create_endpoint(pg_client)

    response = pg_client.post(
        ENDPOINT, data={"email": "dev@example.com", "topics": ["billing", "api"]}
    )

    assert response.status_code == 202
    body = response.json()
    assert body["delivery"] == {"queued": True}

    with sessions() as session:
        submission = session.get(models.Submission, body["submission_id"])
        assert submission is not None
        assert submission.endpoint_id == "contact-form"
        # Repeated values must survive the PostgreSQL json column intact.
        assert submission.fields == {
            "email": ["dev@example.com"],
            "topics": ["billing", "api"],
        }
        assert submission.received_at.tzinfo is not None

        delivery = session.scalars(select(models.WebhookDelivery)).one()
        assert delivery.submission_id == submission.id
        assert delivery.state == DeliveryState.PENDING
        assert delivery.destination_url == endpoint["webhook_url"]
        assert delivery.signing_secret == endpoint["webhook_secret"]


def test_field_order_survives_the_postgresql_json_column(
    pg_client: TestClient, sessions: sessionmaker[Session]
) -> None:
    """
    the column is json rather than jsonb precisely so key order is preserved
    :param pg_client: an API client backed by PostgreSQL
    :param sessions: factory handing out independent connections
    """
    create_endpoint(pg_client, webhook=False)

    pg_client.post(
        ENDPOINT,
        content=b"zebra=1&apple=2&mango=3",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )

    with sessions() as session:
        submission = session.scalars(select(models.Submission)).one()
    assert list(submission.fields) == ["zebra", "apple", "mango"]


def test_an_idempotent_replay_duplicates_nothing_on_postgresql(
    pg_client: TestClient, sessions: sessionmaker[Session]
) -> None:
    create_endpoint(pg_client)
    headers = {"Idempotency-Key": KEY}

    first = pg_client.post(ENDPOINT, data={"email": "dev@example.com"}, headers=headers)
    second = pg_client.post(ENDPOINT, data={"email": "dev@example.com"}, headers=headers)

    assert first.status_code == second.status_code == 202
    assert second.json()["submission_id"] == first.json()["submission_id"]
    assert second.json()["idempotent_replay"] is True
    with sessions() as session:
        assert len(list(session.scalars(select(models.Submission)))) == 1
        assert len(list(session.scalars(select(models.WebhookDelivery)))) == 1


def test_an_idempotency_conflict_is_refused_on_postgresql(
    pg_client: TestClient, sessions: sessionmaker[Session]
) -> None:
    create_endpoint(pg_client)
    headers = {"Idempotency-Key": KEY}
    pg_client.post(ENDPOINT, data={"email": "dev@example.com"}, headers=headers)

    response = pg_client.post(ENDPOINT, data={"email": "other@example.com"}, headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "idempotency_conflict"
    with sessions() as session:
        assert len(list(session.scalars(select(models.Submission)))) == 1


def test_an_endpoint_without_a_webhook_queues_nothing_on_postgresql(
    pg_client: TestClient, sessions: sessionmaker[Session]
) -> None:
    create_endpoint(pg_client, webhook=False)

    response = pg_client.post(ENDPOINT, data={"email": "dev@example.com"})

    assert response.json()["delivery"] == {"queued": False}
    with sessions() as session:
        assert list(session.scalars(select(models.WebhookDelivery))) == []


def test_a_rejected_submission_persists_nothing_on_postgresql(
    pg_client: TestClient, sessions: sessionmaker[Session]
) -> None:
    create_endpoint(pg_client)

    response = pg_client.post(
        ENDPOINT, content=b"", headers={"content-type": "application/x-www-form-urlencoded"}
    )

    assert response.status_code == 422
    with sessions() as session:
        assert list(session.scalars(select(models.Submission))) == []
        assert list(session.scalars(select(models.WebhookDelivery))) == []


def test_the_application_refuses_to_start_against_an_unmigrated_database(
    postgres_url: str,
) -> None:
    """
    startup checks the revision rather than quietly creating what it is missing
    :param postgres_url: a URL on the PostgreSQL server to work against
    """
    from hymical_forms.app import create_app
    from hymical_forms.schema import SchemaNotReady
    from integration.support import IsolatedSettings, temporary_database

    with temporary_database(postgres_url) as url:
        app = create_app(IsolatedSettings(database_url=url))
        try:
            with TestClient(app):
                raise AssertionError("the application started against an empty database")
        except SchemaNotReady as exc:
            assert "alembic upgrade head" in str(exc)
        finally:
            app.state.engine.dispose()

"""
the generated OpenAPI document

The schema is what an integrator reads first, so what it says about the
authentication boundary has to be true. A management route that forgot its
dependency would advertise itself as public here, which is precisely the mistake
worth failing a build over.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Every route that must require a management API key, and every route that must
# not. Adding a management route without adding it here would leave its
# authentication unasserted, so the two lists are also the checklist.
MANAGEMENT_OPERATIONS = [
    ("/endpoints", "post"),
    ("/endpoints", "get"),
    ("/endpoints/{endpoint_id}", "get"),
    ("/endpoints/{endpoint_id}", "patch"),
    ("/deliveries", "get"),
    ("/deliveries/{delivery_id}", "get"),
    ("/deliveries/{delivery_id}/replay", "post"),
    ("/submissions", "get"),
    ("/submissions/export", "get"),
    ("/submissions/{submission_id}", "get"),
]

PUBLIC_OPERATIONS = [
    ("/health", "get"),
    ("/f/{endpoint_id}", "post"),
]


def schema(client: TestClient) -> dict[str, Any]:
    """
    read the OpenAPI document the application generates
    :param client: the client whose application should be described
    :returns: the generated schema
    """
    return cast(FastAPI, client.app).openapi()


def operation(client: TestClient, path: str, method: str) -> dict[str, Any]:
    """
    read one operation out of the generated document
    :param client: the client whose application should be described
    :param path: the templated path the operation is declared on
    :param method: the HTTP method, lowercased
    :returns: the operation object
    """
    paths = schema(client)["paths"]
    assert path in paths, f"{path} is not in the generated schema"
    assert method in paths[path], f"{method.upper()} {path} is not in the generated schema"
    return cast(dict[str, Any], paths[path][method])


def test_every_route_is_declared(client: TestClient) -> None:
    declared = {
        (path, method)
        for path, operations in schema(client)["paths"].items()
        for method in operations
    }

    assert declared == set(MANAGEMENT_OPERATIONS) | set(PUBLIC_OPERATIONS)


@pytest.mark.parametrize(("path", "method"), MANAGEMENT_OPERATIONS)
def test_a_management_route_advertises_bearer_authentication(
    client: TestClient, path: str, method: str
) -> None:
    security = operation(client, path, method).get("security")

    assert security is not None, f"{method.upper()} {path} advertises no authentication"
    assert any("ManagementApiKey" in requirement for requirement in security)


@pytest.mark.parametrize(("path", "method"), PUBLIC_OPERATIONS)
def test_a_public_route_advertises_no_authentication(
    client: TestClient, path: str, method: str
) -> None:
    assert "security" not in operation(client, path, method)


def test_the_security_scheme_is_a_bearer_scheme(client: TestClient) -> None:
    scheme = schema(client)["components"]["securitySchemes"]["ManagementApiKey"]

    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"


@pytest.mark.parametrize(("path", "method"), MANAGEMENT_OPERATIONS)
def test_a_management_route_documents_its_refusal(
    client: TestClient, path: str, method: str
) -> None:
    responses = operation(client, path, method)["responses"]

    assert "401" in responses


def test_the_submission_route_documents_being_rate_limited(client: TestClient) -> None:
    """
    a public route that can answer 429 has to say so, or a caller cannot handle it
    :param client: test client whose app holds the default endpoint
    """
    responses = operation(client, "/f/{endpoint_id}", "post")["responses"]

    assert "429" in responses
    assert "401" not in responses, "the public submission route advertises authentication"


def test_no_response_schema_mentions_a_webhook_signing_secret(client: TestClient) -> None:
    """
    a read model that could name the secret is a leak waiting to be written
    :param client: test client whose app holds the default endpoint
    """
    components = schema(client)["components"]["schemas"]

    exposing = {
        name
        for name, model in components.items()
        if "webhook_secret" in model.get("properties", {})
    }
    # The one model that may name it is the mutation response, where a newly
    # generated secret is handed over exactly once and never read back.
    assert exposing == {"EndpointResponse"}

    for name, model in components.items():
        properties = set(model.get("properties", {}))
        assert "signing_secret" not in properties, name
        assert "key_digest" not in properties, name
        assert "api_key" not in properties, name


def test_the_delivery_views_carry_no_submitted_fields(client: TestClient) -> None:
    components = schema(client)["components"]["schemas"]

    for name in ("DeliveryView", "DeliveryDetail", "DeliveryAttemptView"):
        assert "fields" not in components[name]["properties"], name


def test_a_page_response_has_the_shared_shape(client: TestClient) -> None:
    components = schema(client)["components"]["schemas"]

    for name in ("EndpointPage", "DeliveryPage", "SubmissionPage"):
        assert set(components[name]["properties"]) == {"items", "next_cursor"}, name


def test_a_submission_listing_cannot_name_the_submitted_values(client: TestClient) -> None:
    """
    a summary model with no fields property cannot leak one however the route changes
    :param client: test client whose app holds the default endpoint
    """
    components = schema(client)["components"]["schemas"]

    assert "fields" not in components["SubmissionSummary"]["properties"]
    # The detail model is the one that is meant to carry them.
    assert "fields" in components["SubmissionDetail"]["properties"]


def test_no_submission_response_exposes_an_internal_column(client: TestClient) -> None:
    components = schema(client)["components"]["schemas"]

    for name in ("SubmissionSummary", "SubmissionDetail", "SubmissionExport"):
        properties = set(components[name]["properties"])
        assert "payload_fingerprint" not in properties, name
        assert "idempotency_key" not in properties, name


def test_the_export_route_documents_both_formats(client: TestClient) -> None:
    content = operation(client, "/submissions/export", "get")["responses"]["200"]["content"]

    assert set(content) == {"application/json", "text/csv"}

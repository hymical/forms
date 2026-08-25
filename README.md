<p align="center">
  <img src="docs/images/logo1.png"
       alt="Hymical Forms logo"
       width="600">
</p>

<p align="center">
  Reliable form ingestion and webhook delivery for developers.
</p>

<p align="center">
  <a href="https://hymical.github.io/forms/">
    <img src="https://img.shields.io/badge/docs-hymical.github.io%2Fforms-4169E1"
         alt="Documentation">
  </a>
  <a href="https://github.com/hymical/forms/actions">
    <img src="https://github.com/hymical/forms/actions/workflows/ci.yml/badge.svg"
         alt="CI">
  </a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue"
       alt="Python 3.11+">
  <a href="https://github.com/hymical/forms/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/license-Apache%202.0-blue"
         alt="Apache License 2.0">
  </a>
  <img src="https://img.shields.io/badge/FastAPI-0.141%2B-009688"
       alt="FastAPI">
  <img src="https://img.shields.io/badge/PostgreSQL-production%20database-4169E1"
       alt="PostgreSQL">
</p>

<p align="center">
  <strong><a href="https://hymical.github.io/forms/">Read the documentation</a></strong>
</p>

## Overview

Every project with a contact form, a waitlist, or a feedback box ends up needing
the same small backend: something that accepts an HTML form POST, validates it,
stores it, and forwards it somewhere useful. Writing that once is easy. Running it
reliably, with retries, delivery logs and an audit trail, is not.

Hymical Forms is that backend, self-hostable and open source. Point an HTML
form's `action` at it and every submission is validated, stored, and delivered to
your webhook with an HMAC signature and a bounded retry schedule. The submission
and the obligation to deliver it are written in one database transaction, so a
crash cannot lose work the service already acknowledged.

## Key features

- **Public form ingestion** that works from a plain `<form action="...">`, with
  explicit validation and request limits
- **Idempotent submission retries** through an `Idempotency-Key` header
- **Durable transactional outbox** in PostgreSQL, so a `202` means the delivery is
  already promised
- **Signed webhook delivery**, HMAC-SHA256 over the raw body
- **A separate retrying worker** with leases, exponential backoff and crash
  recovery
- **Management API keys**, created by an operator CLI and stored only as digests
- **Endpoint and delivery operations**: reconfigure, inspect attempt history,
  replay a failed delivery
- **Submission retrieval and export**: browse, filter by endpoint and time, read
  one back, export a filtered range as JSON or CSV
- **Retention cleanup** driven by an operator command, which never deletes a
  submission a delivery could still need
- **Distributed rate limiting** on public ingestion, per source address and per
  endpoint, shared across API processes
- **Alembic migrations** with a startup revision check and model drift tests
- **Real PostgreSQL integration testing**, including genuine concurrency

## Architecture

```mermaid
flowchart TD
    Form["HTML form"] -->|"public submission"| API["FastAPI API process"]
    Operator["Operator"] -->|"authenticated management routes"| API
    API --> DB[("PostgreSQL")]
    DB --> Worker["Delivery worker process"]
    Worker -->|"signed HTTP POST"| Receiver["Your webhook receiver"]
    Worker --> DB
```

The API stores the submission and its delivery job in one transaction and never
makes an outbound request. The worker claims owed deliveries, sends them, and
retries the ones that fail. PostgreSQL is the queue: there is no broker and no
Redis.

See [Architecture](https://hymical.github.io/forms/architecture/overview/).

## Quick start

```bash
git clone https://github.com/hymical/forms.git && cd forms
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

```bash
export FORMS_DATABASE_URL=postgresql+psycopg://forms:forms@localhost:5432/forms
alembic upgrade head
```

```bash
python -m hymical_forms.cli create-key --name local-admin
```

Save the key it prints. It is shown once and stored only as a digest.

```bash
uvicorn hymical_forms.main:app --reload   # terminal one
python -m hymical_forms.worker            # terminal two
```

Register an endpoint, then submit to it:

```bash
curl -X POST http://127.0.0.1:8000/endpoints \
  -H "Authorization: Bearer $HYMICAL_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"id": "contact-form", "name": "Contact form",
       "webhook_url": "https://example.com/hooks/forms"}'
```

```bash
curl -i -X POST http://127.0.0.1:8000/f/contact-form \
  -d email=dev@example.com -d message=hello
```

```json
{
  "submission_id": "sub_48984534f33749c49a88de2d59400dce",
  "endpoint_id": "contact-form",
  "received_at": "2026-08-24T14:34:27.651841Z",
  "field_count": 2,
  "idempotent_replay": false,
  "delivery": { "queued": true }
}
```

Submitting needs no credential: that URL goes straight in an HTML form.

Full walkthrough:
[Getting Started](https://hymical.github.io/forms/getting-started/quick-start/).

## Documentation

| Section | Covers |
| --- | --- |
| [Getting Started](https://hymical.github.io/forms/getting-started/installation/) | Install, configure, migrate, first submission |
| [Guides](https://hymical.github.io/forms/guides/form-ingestion/) | Ingestion, idempotency, webhooks, rate limiting, endpoints, replay, submissions, export |
| [API Reference](https://hymical.github.io/forms/api/authentication/) | Every route, its parameters and responses, and the complete error table |
| [Operations](https://hymical.github.io/forms/operations/worker/) | Worker, migrations, retention, reverse proxy, every configuration variable |
| [Architecture](https://hymical.github.io/forms/architecture/overview/) | Transactional outbox, delivery semantics, concurrency, security |
| [Data handling](https://hymical.github.io/forms/reference/data-handling/) | Where submitted values go, and where they never go |
| [Limitations](https://hymical.github.io/forms/reference/limitations/) | An honest list of what this build does not do yet |

## Project status

**Early development, under active development.** Everything below is implemented
and covered by tests, including a PostgreSQL suite that exercises real
concurrency.

| Capability | Status |
| --- | --- |
| Form ingestion, validation and request limits | Implemented |
| Idempotent submission retries | Implemented |
| Submission persistence and durable delivery queue | Implemented |
| Signed webhook delivery with retries and backoff | Implemented |
| Management API keys and authentication | Implemented |
| Endpoint management | Implemented |
| Delivery inspection and manual replay | Implemented |
| Public ingestion rate limiting | Implemented |
| Schema migrations | Implemented |
| Submission retrieval and filtering | Implemented |
| Submission export, JSON and CSV | Implemented |
| Retention cleanup, operator-run | Implemented |
| Scheduled retention | **Not implemented** |
| Submission search | **Not implemented** |
| Endpoint deletion | **Not implemented** |
| Spam handling, CAPTCHA | **Not implemented** |
| Dashboards | **Not implemented** |

Four things are worth knowing before you deploy it:

- **Delivery is at-least-once, not exactly-once.** Deduplicate on the submission
  `id` in the signed payload.
- **Rate limiting is traffic protection, not spam protection.** It bounds volume
  and has no opinion about content.
- **SSRF protection is partial.** Webhook hostnames are not resolved.
- **Nothing is deleted until you delete it.** Retention is a command an operator
  runs, and it never removes a submission a delivery could still need.

The full list is in
[Limitations](https://hymical.github.io/forms/reference/limitations/).

## Development

```bash
pytest                    # run the test suite
ruff check .              # lint
ruff format --check .     # formatting check
mypy                      # type check
```

`pytest` needs no services: the fast suite runs on in-memory SQLite. A smaller
suite runs against a real PostgreSQL database for locking, constraints and
concurrency, and skips itself unless you point it at one:

```bash
export HYMICAL_TEST_POSTGRES_URL=postgresql+psycopg://forms:forms@localhost:5432/forms_test
pytest tests/integration -m postgres
```

See [Testing](https://hymical.github.io/forms/development/testing/) and
[Contributing](https://hymical.github.io/forms/development/contributing/).

## License

[Apache License 2.0](LICENSE).

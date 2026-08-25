---
hide:
  - navigation
---

<p align="center">
  <img src="images/logo1.png#only-light" alt="Hymical Forms" width="440">
  <img src="images/logo2.png#only-dark" alt="Hymical Forms" width="440">
</p>

<p align="center">
  <strong>Reliable form ingestion and webhook delivery for developers.</strong>
</p>

---

Every project with a contact form, a waitlist, or a feedback box needs the same
small backend: something that accepts an HTML form POST, validates it, stores it,
and forwards it somewhere useful. Writing that once is easy. Running it reliably,
with retries, delivery logs and an audit trail, is not.

Hymical Forms is that backend, self-hostable and open source. Point an HTML
form's `action` at it and every submission is validated, stored, and delivered to
your webhook with an HMAC signature and a bounded retry schedule.

## What you get

<div class="grid cards" markdown>

-   __Public form ingestion__

    A URL you can put straight in a `<form action="...">`. No credential, no
    JavaScript, no CORS dance. Rate limited per source address and per endpoint.

    [Form ingestion](guides/form-ingestion.md)

-   __Delivery that survives a crash__

    A submission and the obligation to deliver it are written in one database
    transaction. Once the API answers `202`, the delivery cannot be lost.

    [Transactional outbox](architecture/transactional-outbox.md)

-   __Signed, retried webhooks__

    HMAC-SHA256 over the raw body, a separate worker process, exponential
    backoff, and a full attempt history you can read back.

    [Webhook delivery](guides/webhooks.md)

-   __Safe retries__

    Send an `Idempotency-Key` and a repeated request returns the original
    submission instead of storing the form twice.

    [Idempotency](guides/idempotency.md)

-   __Submissions you can read back__

    Browse and filter what your forms collected, read one submission in full, and
    export a range as JSON or CSV. Authenticated, always.

    [Browsing submissions](guides/submission-management.md)

-   __Retention you control__

    Nothing is deleted until you run the cleanup command, and it never removes a
    submission a delivery could still need.

    [Retention](operations/retention.md)

</div>

## Requirements

- Python 3.11 or newer
- PostgreSQL, which is the intended production database

SQLite is supported for local experimentation and backs the test suite. It is not
a supported production target.

## Install

```bash
pip install -e ".[dev]"
```

```bash
export FORMS_DATABASE_URL=postgresql+psycopg://forms:forms@localhost:5432/forms
alembic upgrade head
```

Full steps are in [Installation](getting-started/installation.md).

## A first submission

Create an endpoint, then point a form at it:

```html
<form action="http://127.0.0.1:8000/f/contact-form" method="POST">
  <input type="email" name="email" required />
  <textarea name="message"></textarea>
  <button type="submit">Send</button>
</form>
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

The [Quick Start](getting-started/quick-start.md) takes you from a clone to that
response in about five minutes.

## Where to go next

<div class="grid cards" markdown>

-   __Getting Started__

    Install, configure a database, migrate it, create a management key, and
    accept your first submission.

    [Start here](getting-started/quick-start.md)

-   __Guides__

    How ingestion, idempotency, webhooks, rate limiting, endpoint management,
    delivery replay and submission export actually behave.

    [Read the guides](guides/form-ingestion.md)

-   __API Reference__

    Every route, its parameters, its responses, and the complete error table.

    [Browse the API](api/authentication.md)

-   __Operations__

    Running the worker, applying migrations, sweeping expired submissions,
    sitting behind a reverse proxy, and every configuration variable.

    [Operate it](operations/worker.md)

-   __Architecture__

    The outbox, at-least-once delivery, the concurrency model, and the security
    boundaries.

    [Understand it](architecture/overview.md)

-   __Data handling__

    Where a submitted value goes, where it never goes, and what deleting one
    does and does not remove.

    [Handle it carefully](reference/data-handling.md)

-   __Limitations__

    An honest list of what this build does not do yet. Worth reading before you
    deploy it.

    [Know the edges](reference/limitations.md)

</div>

## Project status

**Early development.** The service registers endpoints, stores submissions with
the durable obligation to deliver them, and runs a worker that performs the
signed delivery and retries it. Endpoint management, delivery inspection, manual
replay, public ingestion rate limiting, submission retrieval and export, and
operator-run retention cleanup are all implemented and covered by tests,
including a PostgreSQL suite that exercises real concurrency.

There is no spam protection, no CAPTCHA and no content classification. Rate
limiting bounds volume, not junk. Retention is never automatic: nothing is
deleted until an operator runs the cleanup command. See
[Limitations](reference/limitations.md) for the full picture.

## License

[Apache License 2.0](https://github.com/hymical/forms/blob/main/LICENSE).

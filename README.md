<p align="center">
  <img src="docs/images/logo_symbol_transparent.png"
       alt="Hymical Forms logo"
       width="120">
</p>

<h1 align="center">Hymical Forms</h1>

<p align="center">
  Reliable form ingestion and webhook delivery for developers.
</p>

<p align="center">
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

## The problem

Every project with a contact form, a waitlist, or a feedback box ends up needing
the same small backend: something that accepts an HTML form POST, validates it,
stores it, and forwards it somewhere useful. Writing that once is easy; running
it reliably, with retries, delivery logs, spam handling and retention rules,
is not. Hymical Forms is intended to be that backend, self-hostable and
open-source.

## Project status

**Early development.** This build registers endpoints, stores the submissions
sent to them together with the durable obligation to deliver them, and runs a
separate worker that performs the signed webhook delivery and retries it.

Endpoint and webhook configuration is completely unauthenticated: anyone who can
reach the API can create an endpoint pointing anywhere. There is no rate limiting
and no spam protection, so do not expose this to the public internet.

| Capability                    | Status                    |
| ----------------------------- | ------------------------- |
| Health endpoint               | Implemented               |
| Form ingestion + validation   | Implemented               |
| Request limits + error model  | Implemented               |
| Endpoint registry             | Implemented               |
| Submission persistence        | Implemented               |
| Idempotent retries            | Implemented               |
| Signed webhook delivery       | Implemented               |
| Durable delivery queue        | Implemented               |
| Retries with backoff          | Implemented               |
| Schema migrations             | Implemented               |
| API keys / authentication     | **Not implemented**       |
| Manual delivery replay        | **Not implemented**       |
| Rate limiting, spam handling  | **Not implemented**       |
| Export, retention, dashboards | **Not implemented**       |

## Requirements

- Python 3.11 or newer
- PostgreSQL, which is the intended production database

SQLite is supported for local experimentation and backs the test suite. It is
not a supported production target.

## Install

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

On Windows, activate with `.venv\Scripts\activate` instead.

## Configure

`FORMS_DATABASE_URL` is required and has no default. Set it in the environment
or in a `.env` file in the working directory:

```bash
FORMS_DATABASE_URL=postgresql+psycopg://forms:forms@localhost:5432/forms
```

To try the service without running PostgreSQL:

```bash
FORMS_DATABASE_URL=sqlite:///./forms.db
```

See [`.env.example`](.env.example) for every setting and its default.

## Run

Hymical Forms is two processes sharing one database. Migrate it first, then
start them:

```bash
alembic upgrade head
```

```bash
uvicorn hymical_forms.main:app --reload
```

```bash
python -m hymical_forms.worker
```

The **API** accepts submissions, stores them, and records that a webhook is owed.
It never makes an outbound request. The **worker** claims owed deliveries, sends
them, and retries the ones that fail. Running the API alone is fine: submissions
are still accepted and nothing is lost, they simply wait until a worker exists.

Neither process creates or alters the schema. Both check on startup that the
database is reachable and at the migration revision the build was written
against, and refuse to start otherwise:

```
the database is at migration '0001' but this build expects '0002'.
Run 'alembic upgrade head' before starting.
```

Migrating is an operator action, run when the operator chooses. See
[Schema migrations](#schema-migrations).

## Schema migrations

Alembic owns the schema. Neither the API nor the worker creates or alters a
table: they check on startup that the database is at the revision they were
built against, and stop if it is not.

### A fresh database

```bash
createdb forms
export FORMS_DATABASE_URL=postgresql+psycopg://forms:forms@localhost:5432/forms
alembic upgrade head
```

That is the whole setup. Migrations read `FORMS_DATABASE_URL`, the same setting
the application reads, so there is nothing extra to configure and no credentials
in any tracked file. To migrate a different database without changing your
environment:

```bash
alembic -x database_url=postgresql+psycopg://user:pass@host/other upgrade head
```

### Upgrading an existing database

```bash
alembic upgrade head        # apply everything outstanding
```

Run it before starting the new build. The usual order for a deploy is: stop the
old processes, migrate, start the new ones. Migrating while an old build is
still running is only safe if the change happens to be backwards compatible,
and this project does not promise that for any particular migration.

Useful alongside it:

```bash
alembic current             # what revision is this database at
alembic history --verbose   # what revisions exist
alembic upgrade head --sql  # print the SQL instead of applying it, for review
alembic downgrade -1        # step back one revision
```

`--sql` is worth knowing about: it lets whoever owns the production database
read the DDL before anything touches it.

### SQLite

Migrations run against SQLite too, so local experimentation works the same way:

```bash
export FORMS_DATABASE_URL=sqlite:///./forms.db
alembic upgrade head
```

Migrations that alter a column are written in batch mode, because SQLite cannot
`ALTER` in place and has to rebuild the table instead. This is configured
already; it is not something a migration author has to remember.

### Writing a migration

```bash
alembic revision --autogenerate -m "what changed"
```

**Read what it produces before committing it.** Autogenerate is a starting
point, not an answer: it does not always render custom column types in a usable
way, and it cannot see anything the models do not declare. The PostgreSQL suite
asserts that migrations and models describe the same schema, so drift fails the
build rather than surfacing in production.

Interactive API documentation is served at `http://127.0.0.1:8000/docs`.

## API

### `GET /health`

Reports that the API process is running.

```json
{ "status": "ok", "service": "hymical-forms", "version": "0.1.0" }
```

This is a liveness signal only. It does not check the database, so it stays
answerable while the database is down, which is what makes it useful for
deciding whether to restart the process.

### `POST /endpoints`

Registers a form endpoint. Submissions are only accepted for endpoints that
exist here.

**This route is unauthenticated.** Authentication is deliberately out of scope
for now, so keep the service on a private network.

```bash
curl -X POST http://127.0.0.1:8000/endpoints \
  -H 'Content-Type: application/json' \
  -d '{"id": "contact-form", "name": "Contact form",
       "webhook_url": "https://example.com/hooks/forms"}'
```

| Field         | Required | Meaning                                            |
| ------------- | -------- | -------------------------------------------------- |
| `id`          | yes      | The public identifier the endpoint answers on      |
| `name`        | yes      | Human-readable label, 1 to 200 characters          |
| `is_active`   | no       | Whether it accepts submissions, defaults to `true` |
| `webhook_url` | no       | Where accepted submissions are delivered           |

**Endpoint IDs** are supplied by you, not generated, because the ID appears in
the `action` URL of your HTML form and a memorable one is worth more than an
opaque one. An ID is 3 to 64 characters of lowercase ASCII letters, digits, `-`
and `_`, and must start and end with a letter or digit. It is also the primary
key, so it cannot be changed later.

Returns `201 Created`:

```json
{
  "id": "contact-form",
  "name": "Contact form",
  "is_active": true,
  "created_at": "2026-08-24T14:34:27.432598Z",
  "webhook_url": "https://example.com/hooks/forms",
  "webhook_secret": "whsec_6f1c...  (64 hex characters)"
}
```

> **Save `webhook_secret` now.** It is generated by the server, returned only in
> this response, and there is no route that reads it back. Losing it means
> creating a new endpoint.

Reusing an ID returns `409 endpoint_already_exists`. There is no route to list,
update or delete endpoints yet, so a webhook can only be configured at creation
time.

### `POST /f/{endpoint_id}`

Accepts a form submission for a registered endpoint and stores it.

A submission to an ID that does not exist is rejected with
`404 endpoint_not_found`, and one to an inactive endpoint with
`409 endpoint_inactive`. Neither leaves anything in the database.

**Content types.** `application/x-www-form-urlencoded` and
`multipart/form-data` are both accepted, so a plain HTML `<form>` works
unchanged. File uploads are not: a multipart part carrying a file is rejected
rather than silently dropped. Anything else is rejected with `415`.

**Repeated field names**, such as checkbox groups and multi-selects, are
preserved in order, both in the response count and in storage. No submitted
value is discarded.

A successful request returns `202 Accepted`. The status is deliberately not
`201`: the submission is stored and any delivery it owes is queued, but that
delivery has not happened yet.

```json
{
  "submission_id": "sub_48984534f33749c49a88de2d59400dce",
  "endpoint_id": "contact-form",
  "received_at": "2026-08-24T14:34:27.651841Z",
  "field_count": 3,
  "idempotent_replay": false,
  "delivery": { "queued": true }
}
```

Submitted values are not echoed back, because the client already has them.

`delivery.queued` says whether a webhook delivery is owed for this submission.
No outbound request is made during this request, so the response says nothing
about whether a destination is reachable: that is the worker's business, and a
destination being down can no longer affect whether a form is accepted.

### Retrying safely with `Idempotency-Key`

A client that never sees a response cannot tell whether the submission landed.
Send an `Idempotency-Key` header and the retry becomes safe: the second request
returns the result of the first instead of storing the form twice.

```bash
KEY=$(uuidgen)
curl -i -X POST http://127.0.0.1:8000/f/contact-form \
  -H "Idempotency-Key: $KEY" \
  -d email=dev@example.com -d message=hello
```

The header is optional. Without it, behaviour is unchanged and every accepted
request stores a new submission.

**Retry semantics.** Repeating the same key on the same endpoint with the same
content returns `202` with the *original* `submission_id` and `received_at`, and
`"idempotent_replay": true`. Only one row is ever stored, and this holds even
when the retries arrive at the same instant: the database, not the application,
is what decides the winner.

The server does not retry anything on your behalf. It only makes your retries
safe.

**Conflict semantics.** Reusing a key on the same endpoint with *different*
content is rejected with `409 idempotency_conflict`. The stored submission is
left exactly as it was, and the response never describes its contents.

Content is compared by a SHA-256 fingerprint of the normalized fields. Because
field order and repeated values are meaningful to this service, they are
meaningful to the fingerprint too: `a=1&b=2` and `b=2&a=1` are different
payloads and will conflict. The generated submission ID and the received
timestamp are excluded, so an honest retry always matches.

**Scope.** A key belongs to one endpoint. The same key may be used once per
endpoint without conflicting, and there is no expiry: a key is spent for as long
as its submission is stored.

**Key format.** 16 to 255 printable ASCII characters with no spaces, which
accepts UUIDs, hex, base64 and base64url. Anything else is rejected with
`400 invalid_idempotency_key`, including a header that is present but empty.

The 16-character floor exists because keys are endpoint-scoped and this API is
unauthenticated, so every client of an endpoint shares one key space. A short or
predictable key would collide with a stranger's submission. Use a random value.

### Webhook delivery

If an endpoint has a `webhook_url`, accepting a submission also writes a durable
delivery record in the **same transaction**. Nothing is sent during the form
request. A worker picks the delivery up, sends it, and retries it if it fails.

That transaction is the whole reliability claim. Once `POST /f/{endpoint_id}`
answers `202`, the submission is stored *and* the obligation to deliver it is
stored. A crash at any point after that cannot lose the delivery, because it is
a row rather than a thing the API process was about to do.

The submission response reports only whether work was queued:

```json
"delivery": { "queued": true }
```

`queued` is false when the endpoint has no webhook. An idempotent replay reports
`true` and does **not** queue a second delivery: one submission owes at most one
delivery, enforced by a unique constraint on the submission.

#### Payload

```json
{
  "type": "submission.received",
  "submission": {
    "id": "sub_48984534f33749c49a88de2d59400dce",
    "endpoint_id": "contact-form",
    "received_at": "2026-08-24T14:34:27.651841Z",
    "fields": {
      "email": ["user@example.com"],
      "topics": ["billing", "api"]
    }
  }
}
```

Every field value is a list, even when only one value was submitted, so a
receiver never has to guess whether a field is single or multi valued. The
signing secret is never part of the payload.

#### Verifying the signature

Signing is unchanged from the previous release. Each request carries a
`Hymical-Signature` header:

```
Hymical-Signature: v1=9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08
```

The digest is HMAC-SHA256 of the **raw request body**, keyed with the endpoint's
`webhook_secret`. Verify against the exact bytes you received, before parsing
the JSON: re-serializing the payload will produce different bytes and fail.

```python
import hashlib
import hmac


def verify(raw_body: bytes, header: str, secret: str) -> bool:
    version, _, digest = header.partition("=")
    if version != "v1":
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, expected)
```

Use a constant-time comparison, as `hmac.compare_digest` does above. The `v1=`
prefix exists so a future scheme can be added without breaking receivers that
only understand this one.

A delivery keeps the destination and secret that were configured when the
submission was accepted. Changing an endpoint's webhook does not redirect
deliveries that are already queued, and does not leave a queued payload signed
with a secret its receiver never had.

#### Delivery states

| State        | Meaning                                                 |
| ------------ | ------------------------------------------------------- |
| `pending`    | Owed, waiting for its next attempt time                  |
| `processing` | Claimed by a worker, holding a lease                     |
| `delivered`  | A destination answered `2xx`. Terminal                   |
| `failed`     | Given up on. Terminal                                    |

#### What retries, and what does not

| Outcome                            | Retried |
| ---------------------------------- | ------- |
| Connection failure                  | yes     |
| Timeout                             | yes     |
| HTTP `5xx`                          | yes     |
| HTTP `408`, `425`, `429`            | yes     |
| HTTP `2xx`                          | delivered |
| Any other `4xx`, including `409`    | no      |
| `3xx`                               | no      |

Ordinary `4xx` responses are final because repeating a request the receiver
called malformed, unauthorized or missing will not repair it. `409` is treated
as final too: from a webhook receiver it almost always means "I already have
this event". Redirects are still not followed, and a `3xx` is a misconfiguration
rather than a passing problem, so it is final as well.

#### Retry schedule

Delivery is attempted immediately, then backs off by doubling, capped:

| Attempt | Waits before it |
| ------- | --------------- |
| 1       | none            |
| 2       | 10s             |
| 3       | 20s             |
| 4       | 40s             |
| 5       | 80s             |

After `FORMS_WEBHOOK_MAX_ATTEMPTS` (default 5) the delivery becomes `failed` and
is never retried. There is no jitter: the schedule is deliberately exactly
predictable. Every attempt, including the last, stays in `delivery_attempts`.

#### At-least-once, not exactly-once

A worker claims a delivery by taking a lease on it. If the worker dies, the
lease expires and another worker picks the delivery up, which is what stops a
crash from stranding work in `processing` forever.

**This means duplicate delivery is possible.** A worker that sends successfully
and then dies before recording that success leaves a delivery that looks unsent,
and the next worker sends it again. No queue can close this window on its own;
it needs the receiver's cooperation. Use the `id` in the signed payload to
ignore an event you have already processed. Hymical Forms does not offer
exactly-once delivery and there is no message broker involved: PostgreSQL is
the queue.

#### What is recorded

`webhook_deliveries` holds one row per logical delivery: its state, how many
attempts it has had, when it is next due, and when it finished.
`delivery_attempts` holds one row per request that actually went out, numbered,
with the outcome, the HTTP status when there was one, and a bounded failure
message. Response bodies are **not** stored, and neither is the signing secret.
A job that is inspected and found not due records nothing. There is no API to
read either table yet; query them directly.

#### Destinations that are refused

A webhook URL must use `http` or `https` and must not name a loopback, private,
link-local, multicast, reserved or unspecified address. That covers `localhost`,
`127.0.0.1`, `[::1]`, `[::ffff:127.0.0.1]`, `10.0.0.0/8`, `192.168.0.0/16`, and
the `169.254.169.254` cloud metadata endpoint. Rejections return
`422 invalid_webhook_url`.

This is **not** complete SSRF protection; see [Limitations](#limitations).

For local development, `FORMS_ALLOW_PRIVATE_WEBHOOK_TARGETS=true` lifts the
address restriction so you can point a webhook at a server on your own machine.
Do not enable it in production.

### Try it

```bash
curl -X POST http://127.0.0.1:8000/endpoints \
  -H 'Content-Type: application/json' \
  -d '{"id": "contact-form", "name": "Contact form"}'
```

```bash
curl -i -X POST http://127.0.0.1:8000/f/contact-form -d email=dev@example.com -d message=hello
```

Or from a browser, against a locally running server:

```html
<form action="http://127.0.0.1:8000/f/contact-form" method="POST">
  <input type="email" name="email" required />
  <textarea name="message"></textarea>
  <button type="submit">Send</button>
</form>
```

### Errors

Every non-2xx response uses one envelope. `code` is stable and
machine-readable; `details` appears only when there is something concrete to
add.

```json
{
  "error": {
    "code": "too_many_fields",
    "message": "Submission carries 120 fields, which exceeds the limit of 100.",
    "details": { "limit": 100, "received": 120 }
  }
}
```

| Status | `code`                     | Cause                                                    |
| ------ | -------------------------- | -------------------------------------------------------- |
| 400    | `malformed_form_body`      | Body does not parse as the declared content type          |
| 400    | `invalid_idempotency_key`  | `Idempotency-Key` header breaks the key format rules      |
| 404    | `invalid_endpoint_id`      | Submission path is not a well-formed endpoint ID          |
| 404    | `endpoint_not_found`       | Endpoint ID is well formed but no such endpoint exists    |
| 404    | `not_found`                | Unknown path                                              |
| 405    | `method_not_allowed`       | Wrong method for a known path                             |
| 409    | `endpoint_inactive`        | Endpoint exists but is not accepting submissions          |
| 409    | `endpoint_already_exists`  | Endpoint ID is already taken                              |
| 409    | `idempotency_conflict`     | Idempotency key already used for different content        |
| 413    | `request_body_too_large`   | Body exceeded `FORMS_MAX_BODY_BYTES`                      |
| 415    | `unsupported_media_type`   | Content type is not a supported form encoding             |
| 422    | `empty_submission`         | No fields were submitted                                  |
| 422    | `invalid_endpoint_id`      | Endpoint ID in a request body breaks the ID rules         |
| 422    | `invalid_request`          | Request body failed schema validation                     |
| 422    | `invalid_webhook_url`      | Webhook destination is malformed or not permitted         |
| 422    | `file_upload_not_supported`| A multipart part carried a file                           |
| 422    | ingestion rule codes       | See below                                                 |
| 500    | `internal_error`           | Unexpected failure; no internals are exposed              |
| 503    | `storage_unavailable`      | The database could not be reached or written to           |

Ingestion rule codes are `too_many_fields`, `field_name_too_long`,
`field_value_too_long`, `invalid_field_name` and `invalid_field_value`.

`invalid_endpoint_id` carries a different status depending on where the ID came
from: `404` when it arrived as a submission path that addresses nothing, `422`
when it arrived as a field in a request body.

## Configuration

All settings are read from `FORMS_`-prefixed environment variables, or from a
`.env` file in the working directory.

| Variable                       | Default    | Meaning                                 |
| ------------------------------ | ---------- | --------------------------------------- |
| `FORMS_DATABASE_URL`           | *required* | SQLAlchemy database URL                 |
| `FORMS_MAX_BODY_BYTES`         | `262144`   | Largest accepted request body, in bytes |
| `FORMS_MAX_FIELDS`             | `100`      | Largest number of name/value pairs      |
| `FORMS_MAX_FIELD_NAME_LENGTH`  | `128`      | Largest field name, in characters       |
| `FORMS_MAX_FIELD_VALUE_LENGTH` | `16384`    | Largest field value, in characters      |
| `FORMS_WEBHOOK_CONNECT_TIMEOUT_SECONDS` | `5`  | Wait for a webhook to accept a connection |
| `FORMS_WEBHOOK_READ_TIMEOUT_SECONDS`    | `10` | Wait for a webhook to respond             |
| `FORMS_ALLOW_PRIVATE_WEBHOOK_TARGETS`   | `false` | Permit loopback and private webhook targets. Development only |
| `FORMS_WEBHOOK_MAX_ATTEMPTS`            | `5`     | Attempts before a delivery is given up on |
| `FORMS_WEBHOOK_RETRY_INITIAL_SECONDS`   | `10`    | Wait before the second attempt; later waits double |
| `FORMS_WEBHOOK_RETRY_MAX_SECONDS`       | `3600`  | Cap on the wait between attempts           |
| `FORMS_WORKER_POLL_SECONDS`             | `1`     | How often an idle worker looks for work    |
| `FORMS_WORKER_BATCH_SIZE`               | `10`    | Deliveries a worker claims at once         |
| `FORMS_WORKER_LEASE_SECONDS`            | `60`    | How long a worker's claim holds            |

## Development

```bash
pytest                    # run the test suite
ruff check .              # lint
ruff format --check .     # formatting check
mypy                      # type check
```

### Two test layers

Most tests run against an in-memory SQLite database, one per test, so `pytest`
needs no services and leaves nothing behind. Their schema is built from the
models and stamped as migrated, rather than replayed migration by migration,
because doing that a few hundred times would cost far more than it proves.

A smaller suite under `tests/integration/` runs against a real PostgreSQL
database, for the things SQLite cannot model honestly: `SELECT ... FOR UPDATE
SKIP LOCKED`, real constraint enforcement, and genuinely concurrent worker
sessions. It skips itself unless you point it at a database it may destroy:

```bash
export HYMICAL_TEST_POSTGRES_URL=postgresql+psycopg://forms:forms@localhost:5432/forms_test
pytest tests/integration -m postgres
```

One of those tests asserts that the migrations and the models describe the same
schema, which is what keeps the fast suite's shortcut honest.

CI runs the lint, format and type checks once, the fast suite across Python
3.11 to 3.13, and the PostgreSQL suite once against a PostgreSQL 17 service.

### Layout

```
src/hymical_forms/
  app.py            application assembly and startup
  config.py         typed settings
  db.py             engine and session lifecycle
  errors.py         the shared JSON error envelope
  delivery.py       the outbound webhook request itself
  ingestion.py      domain rules: endpoint IDs, submission validation
  middleware.py     request body size limit
  models.py         the persisted schema
  storage.py        queries and writes
  webhooks.py       webhook rules: URL validation, payload, signature, retry policy
  worker.py         the delivery worker process
  schema.py         the boundary between the application and Alembic
  main.py           ASGI entrypoint
  api/              HTTP routes and response models
  migrations/       Alembic environment and revisions
```

`ingestion.py` and `webhooks.py` hold the domain rules and know nothing about
HTTP or the database. `models.py` and `storage.py` are the only modules that
write queries, and `delivery.py` is the only one that makes an outbound request.
`api/` translates requests into domain rules and storage calls, and their
outcomes into responses. `worker.py` is a separate process and shares only the
database with the API.

### Storage notes

Submission fields are stored as a JSON object mapping each field name to the
list of values submitted under it, which is how repeated names survive intact.
On PostgreSQL the column is `json` rather than `jsonb`, because `jsonb`
normalises object key order and would silently reorder a form's fields.

Each request runs in one transaction, committed explicitly rather than in the
session teardown, so a failure becomes an error response instead of a success
for a row that never landed. A failure anywhere before the commit leaves the
database untouched.

An idempotency key is unique per endpoint through a database constraint on
`(endpoint_id, idempotency_key)`. Both PostgreSQL and SQLite treat NULLs in a
unique constraint as distinct, so submissions sent without a key stay
unrestricted without needing a partial index. A lookup before inserting is only
an optimisation for the common retry; when two requests race, one insert loses
on the constraint, rolls back and reads the winner's row. A `CHECK` constraint
keeps the key and its fingerprint either both set or both absent.

A submission and the delivery it owes are written in one transaction. Either
both land or neither does, so there is no state in which a form was accepted but
the promise to deliver it went missing, and none in which delivery work exists
for a submission that does not.

The network call happens later, in the worker, with no transaction open. Holding
a database transaction across a call to somebody else's server would tie the
connection pool to how fast that server answers.

Workers claim deliveries with `SELECT ... FOR UPDATE SKIP LOCKED` on PostgreSQL,
so two workers scanning at once are handed different rows rather than fighting
over the same one. SQLite has no such locking and silently ignores `FOR UPDATE`,
so the claim also performs a conditional update and treats a row as claimed only
if that update matched. That guard is redundant under `SKIP LOCKED` and is what
makes the claim safe on SQLite.

This is covered by real integration tests: concurrent PostgreSQL sessions claim
disjoint work, a row another worker holds is skipped rather than waited on, and
an expired lease becomes reclaimable by exactly one worker.

## Limitations

- **Delivery is at-least-once, never exactly-once.** A worker that delivers
  successfully and dies before recording it will have its lease expire, and the
  next worker will deliver the same event again. Deduplicate on the submission
  `id` in the signed payload.
- **Only one migration exists so far.** The upgrade path is real and tested, but
  it has only ever been exercised from an empty database to the baseline. Nothing
  has yet had to migrate data it cared about.
- **A failed delivery is final and cannot be replayed.** Once a delivery reaches
  `failed`, nothing retries it and there is no manual replay route.
- **The lease must outlast a delivery attempt.** A batch is delivered
  concurrently, so it takes about as long as its slowest single delivery rather
  than the sum, but if `FORMS_WORKER_LEASE_SECONDS` were set below the connect
  and read timeouts combined, another worker could claim a delivery that is still
  in flight and send it twice. The defaults leave a wide margin; keep it that way
  if you change them.
- **SSRF protection is partial.** Destination URLs are checked for scheme and for
  literal internal addresses, and redirects are not followed. Hostnames are
  **not** resolved, so a name that resolves to a private address still passes,
  and DNS rebinding is not addressed at all. Closing this properly means
  resolving at request time and pinning the connection to the validated address.
  Treat the current checks as a guardrail against mistakes, not a defence against
  an attacker who can configure endpoints.
- **No authentication.** Anyone who can reach the API can create an endpoint,
  point its webhook anywhere, and post to any active one. There is no rate
  limiting or spam protection.
- **A webhook can only be set when the endpoint is created.** There is no route
  to change a destination or rotate a signing secret.
- **No API for delivery attempts.** They are recorded, but reading them means
  querying the database directly.
- **Migrations are applied by hand, one command at a time.** There is no
  zero-downtime story and none is claimed: a migration that rewrites a table
  will lock it, and a build whose expected revision does not match the database
  refuses to start rather than serving against a schema it does not understand.
  Plan a deploy as migrate-then-restart.
- **No way to read submissions back over the API.** They are stored, but
  retrieval, export and retention are not implemented.
- **No route to list, update or delete endpoints.**
- **No file uploads.** Multipart text fields are accepted; file parts are
  rejected.
- **`multipart/form-data` bodies are buffered in memory,** bounded by
  `FORMS_MAX_BODY_BYTES`.
- A rejected submission reveals whether an endpoint ID exists, which allows
  enumeration. This is unavoidable while the API is unauthenticated.
- **Idempotency keys never expire.** A key stays spent for as long as its
  submission is stored, so the table only grows. Expiry belongs with retention.
- **Idempotency keys are shared across all clients of an endpoint,** because
  there is nothing to scope them to yet. Guessing another client's key returns
  that submission's ID and timestamp, though never its contents. Random keys of
  the required length make this impractical, and API keys will close it properly.
- **A replay is only recognised once the first attempt has committed.** A retry
  sent while the original is still in flight is treated as a concurrent request,
  which is safe, but a retry sent after the original *failed* is a new
  submission, which is correct.
- Submission IDs are opaque and not yet guaranteed stable in format.

## License

[Apache License 2.0](LICENSE).

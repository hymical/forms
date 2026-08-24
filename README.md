<p align="center">
  <img src="docs/images/logo_symbol_transparent.png"
       alt="Hymical Forms logo"
       width="120">
</p>

<h1 align="center">Hymical Forms</h1>

<p align="center">
  Reliable form ingestion and webhook delivery for developers.
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
sent to them, and delivers each one once to a signed webhook. There are no
retries yet, so delivery is best effort.

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
| Signed webhook delivery       | One immediate attempt     |
| API keys / authentication     | **Not implemented**       |
| Webhook retries and backoff   | **Not implemented**       |
| Rate limiting, spam handling  | **Not implemented**       |
| Schema migrations             | **Not implemented**       |
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

```bash
uvicorn hymical_forms.main:app --reload
```

Missing tables are created at startup, so an empty database is enough to begin.
Startup fails if the database cannot be reached, rather than serving requests
that would only fail later. There is no migration framework yet, so startup
never alters a table that already exists; see [Limitations](#limitations).

> **Upgrading from an earlier build:** the schema has changed twice. The
> `submissions` table gained `idempotency_key` and `payload_fingerprint`, the
> `endpoints` table gained `webhook_url` and `webhook_secret`, and
> `delivery_attempts` is new. Startup creates missing tables but never alters an
> existing one, so a database created before these changes has to be recreated.
> For local SQLite, delete the file and restart. For PostgreSQL,
> `DROP TABLE delivery_attempts, submissions, endpoints;` and restart. There is
> no in-place upgrade path, and there is no data worth keeping in a development
> database.

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
`201`: the submission is stored, and delivering it onwards is a separate concern
that may not have finished succeeding.

```json
{
  "submission_id": "sub_48984534f33749c49a88de2d59400dce",
  "endpoint_id": "contact-form",
  "received_at": "2026-08-24T14:34:27.651841Z",
  "field_count": 3,
  "idempotent_replay": false,
  "delivery": { "attempted": true, "outcome": "succeeded" }
}
```

Submitted values are not echoed back, because the client already has them.

`delivery` reports what happened to this endpoint's webhook. `attempted` is
false when the endpoint has no webhook and on an idempotent replay, which never
redelivers; `outcome` is null in both cases. **A failed delivery does not change
the `202`**: the submission is already durable, and losing it because someone
else's server was down would be the wrong trade.

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

If an endpoint has a `webhook_url`, each accepted submission is delivered to it
once, immediately, as a signed JSON POST.

**One attempt, no retries.** A submission gets exactly one delivery attempt.
There is no retry schedule, no backoff, no queue and no dead-letter handling, so
a destination that is down when a form is submitted misses that submission. The
submission itself is still stored. Retries are the next thing this layer needs,
and until they exist, delivery is best effort.

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

Each request carries a `Hymical-Signature` header:

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

There is no timestamp in the signature. To guard against replay, use the
`id` and `received_at` inside the signed payload: submission IDs are unique, so
ignoring one you have already processed is both replay protection and protection
against a future retry delivering twice.

#### What counts as success

| Outcome         | Meaning                                                    |
| --------------- | ---------------------------------------------------------- |
| `succeeded`     | The destination answered `2xx`                              |
| `http_error`    | The destination answered anything else, including `3xx`     |
| `timeout`       | The destination did not connect or answer in time           |
| `network_error` | The connection could not be made at all                     |

**Redirects are not followed.** A `3xx` is recorded as `http_error`. Following
redirects would let a destination bounce the request to an address that URL
validation refused, which is the usual way SSRF protection gets walked around.

Timeouts default to 5 seconds to connect and 10 seconds to respond, both
configurable. A slow destination cannot stall form ingestion beyond that.

#### What is recorded

Every attempt writes a row to `delivery_attempts`: the attempt ID, the
submission, the URL used, the timestamp, the outcome, the HTTP status when there
was one, and a bounded failure message. Response bodies are **not** stored, and
neither is the signing secret. There is no API to read these back yet; query the
table directly.

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

## Development

```bash
pytest                    # run the test suite
ruff check .              # lint
ruff format --check .     # formatting check
mypy                      # type check
```

Tests run against an in-memory SQLite database, one per test, so no database
server is needed and nothing is left behind.

### Layout

```
src/hymical_forms/
  app.py            application assembly and startup
  config.py         typed settings
  db.py             engine, session, and schema lifecycle
  errors.py         the shared JSON error envelope
  delivery.py       the single outbound webhook attempt
  ingestion.py      domain rules: endpoint IDs, submission validation
  middleware.py     request body size limit
  models.py         the persisted schema
  storage.py        queries and writes
  webhooks.py       webhook rules: URL validation, payload, signature
  main.py           ASGI entrypoint
  api/              HTTP routes and response models
```

`ingestion.py` and `webhooks.py` hold the domain rules and know nothing about
HTTP or the database. `models.py` and `storage.py` are the only modules that
write queries, and `delivery.py` is the only one that makes an outbound request.
`api/` translates requests into domain rules and storage calls, and their
outcomes into responses.

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

Webhook delivery is deliberately outside the submission's transaction. The
submission is committed first, then the network call is made with no transaction
held, then the attempt is recorded in a transaction of its own. Holding a
database transaction open across a call to somebody else's server would tie the
connection pool to how fast that server answers.

That ordering has one consequence worth naming: if recording the attempt fails
after the webhook has already been sent, the request still returns `202`. The
submission is durable and the delivery did happen, so reporting failure would be
untrue and would invite a retry that delivers a second time. The lost record is
logged for an operator.

## Limitations

- **No webhook retries.** Each submission gets exactly one delivery attempt. If
  it fails, nothing re-sends it and there is no way to replay it. If the process
  dies between committing a submission and delivering it, that delivery never
  happens. Delivery is best effort until retries exist.
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
- **No migration framework.** Startup creates missing tables and nothing else,
  so any future change to an existing column has to be applied by hand.
  Alembic will arrive when the schema first needs to change.
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

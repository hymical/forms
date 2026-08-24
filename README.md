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

**Early development.** This build registers endpoints and stores the
submissions sent to them. Nothing is delivered onwards yet.

Endpoint management is completely unauthenticated: anyone who can reach the API
can create an endpoint. There is no rate limiting and no spam protection, so do
not expose this to the public internet.

| Capability                    | Status                    |
| ----------------------------- | ------------------------- |
| Health endpoint               | Implemented               |
| Form ingestion + validation   | Implemented               |
| Request limits + error model  | Implemented               |
| Endpoint registry             | Implemented               |
| Submission persistence        | Implemented               |
| API keys / authentication     | **Not implemented**       |
| Webhook delivery and retries  | **Not implemented**       |
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
  -d '{"id": "contact-form", "name": "Contact form"}'
```

| Field       | Required | Meaning                                            |
| ----------- | -------- | -------------------------------------------------- |
| `id`        | yes      | The public identifier the endpoint answers on      |
| `name`      | yes      | Human-readable label, 1 to 200 characters          |
| `is_active` | no       | Whether it accepts submissions, defaults to `true` |

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
  "created_at": "2026-08-24T14:34:27.432598Z"
}
```

Reusing an ID returns `409 endpoint_already_exists`. There is no route to list,
update or delete endpoints yet.

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
`201`: the submission is stored, but the delivery it was accepted for has not
happened yet.

```json
{
  "submission_id": "sub_48984534f33749c49a88de2d59400dce",
  "endpoint_id": "contact-form",
  "received_at": "2026-08-24T14:34:27.651841Z",
  "field_count": 3
}
```

Submitted values are not echoed back, because the client already has them.

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
| 404    | `invalid_endpoint_id`      | Submission path is not a well-formed endpoint ID          |
| 404    | `endpoint_not_found`       | Endpoint ID is well formed but no such endpoint exists    |
| 404    | `not_found`                | Unknown path                                              |
| 405    | `method_not_allowed`       | Wrong method for a known path                             |
| 409    | `endpoint_inactive`        | Endpoint exists but is not accepting submissions          |
| 409    | `endpoint_already_exists`  | Endpoint ID is already taken                              |
| 413    | `request_body_too_large`   | Body exceeded `FORMS_MAX_BODY_BYTES`                      |
| 415    | `unsupported_media_type`   | Content type is not a supported form encoding             |
| 422    | `empty_submission`         | No fields were submitted                                  |
| 422    | `invalid_endpoint_id`      | Endpoint ID in a request body breaks the ID rules         |
| 422    | `invalid_request`          | Request body failed schema validation                     |
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
  ingestion.py      domain rules: endpoint IDs, submission validation
  middleware.py     request body size limit
  models.py         the persisted schema
  storage.py        queries and writes
  main.py           ASGI entrypoint
  api/              HTTP routes and response models
```

`ingestion.py` holds the domain rules and knows nothing about HTTP or the
database. `models.py` and `storage.py` are the only modules that write queries.
`api/` translates requests into domain rules and storage calls, and their
outcomes into responses.

### Storage notes

Submission fields are stored as a JSON object mapping each field name to the
list of values submitted under it, which is how repeated names survive intact.
On PostgreSQL the column is `json` rather than `jsonb`, because `jsonb`
normalises object key order and would silently reorder a form's fields.

Each request runs in one transaction, committed explicitly by the route handler.
A failure anywhere before that commit leaves the database untouched.

## Limitations

- **Nothing is delivered.** There are no webhooks, retries or delivery logs.
- **No authentication.** Anyone who can reach the API can create an endpoint and
  post to any active one. There is no rate limiting or spam protection.
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
- Submission IDs are opaque and not yet guaranteed stable in format.

## License

[Apache License 2.0](LICENSE).

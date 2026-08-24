# Hymical Forms

Reliable form ingestion and webhook delivery for developers.

## The problem

Every project with a contact form, a waitlist, or a feedback box ends up needing
the same small backend: something that accepts an HTML form POST, validates it,
stores it, and forwards it somewhere useful. Writing that once is easy; running
it reliably — with retries, delivery logs, spam handling and retention rules —
is not. Hymical Forms is intended to be that backend, self-hostable and
open-source.

## Project status

**Early development.** This build implements the ingestion boundary only.

A submission is parsed, validated and acknowledged — and then discarded.
Nothing is persisted and nothing is delivered anywhere. There is no
authentication, no rate limiting, and no spam protection, so do not expose this
to the public internet.

| Capability                    | Status                    |
| ----------------------------- | ------------------------- |
| Health endpoint               | Implemented               |
| Form ingestion + validation   | Implemented               |
| Request limits + error model  | Implemented               |
| Persistence                   | **Not implemented**       |
| API keys / authentication     | **Not implemented**       |
| Webhook delivery and retries  | **Not implemented**       |
| Rate limiting, spam handling  | **Not implemented**       |
| Export, retention, dashboards | **Not implemented**       |

## Requirements

Python 3.11 or newer.

## Install

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

On Windows, activate with `.venv\Scripts\activate` instead.

## Run

```bash
uvicorn hymical_forms.main:app --reload
```

Interactive API documentation is served at `http://127.0.0.1:8000/docs`.

## API

### `GET /health`

Reports that the API process is running.

```json
{ "status": "ok", "service": "hymical-forms", "version": "0.1.0" }
```

This is a liveness signal only. Hymical Forms has no external dependencies yet,
so there is nothing that readiness could report separately.

### `POST /f/{endpoint_id}`

Accepts a form submission.

**Endpoint IDs** are 3–64 characters of lowercase ASCII letters, digits, `-` and
`_`, and must start and end with a letter or digit. There is no endpoint
registry yet, so any syntactically valid ID is addressable; a malformed one is
rejected with `404 invalid_endpoint_id`.

**Content types.** `application/x-www-form-urlencoded` and
`multipart/form-data` are both accepted, so a plain HTML `<form>` works
unchanged. File uploads are not: a multipart part carrying a file is rejected
rather than silently dropped. Anything else is rejected with `415`.

**Repeated field names** — checkbox groups, multi-selects — are preserved in
order. No submitted value is discarded.

A successful request returns `202 Accepted`. The status is deliberately not
`201`: the submission is acknowledged as received and well-formed, but nothing
was created, stored or delivered.

```json
{
  "submission_id": "sub_67f039efbe774e45ab0e93685eb2d0b6",
  "endpoint_id": "contact-form",
  "received_at": "2026-08-24T13:59:23.891632Z",
  "field_count": 2
}
```

Submitted values are not echoed back — the client already has them.

### Try it

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

| Status | `code`                                                                                          | Cause                                            |
| ------ | ----------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| 400    | `malformed_form_body`                                                                             | Body does not parse as the declared content type |
| 404    | `invalid_endpoint_id`                                                                             | Path is not a well-formed endpoint ID            |
| 404    | `not_found`                                                                                       | Unknown path                                     |
| 405    | `method_not_allowed`                                                                              | Wrong method for a known path                    |
| 413    | `request_body_too_large`                                                                          | Body exceeded `FORMS_MAX_BODY_BYTES`             |
| 415    | `unsupported_media_type`                                                                          | Content type is not a supported form encoding    |
| 422    | `empty_submission`                                                                                | No fields were submitted                         |
| 422    | `too_many_fields`, `field_name_too_long`, `field_value_too_long`, `invalid_field_name`, `invalid_field_value` | A field breached an ingestion rule    |
| 422    | `file_upload_not_supported`                                                                       | A multipart part carried a file                  |
| 500    | `internal_error`                                                                                  | Unexpected failure; no internals are exposed     |

## Configuration

All settings are read from `FORMS_`-prefixed environment variables, or from a
`.env` file in the working directory. See [`.env.example`](.env.example) for the
full list and defaults.

| Variable                        | Default  | Meaning                                    |
| ------------------------------- | -------- | ------------------------------------------ |
| `FORMS_MAX_BODY_BYTES`          | `262144` | Largest accepted request body, in bytes    |
| `FORMS_MAX_FIELDS`              | `100`    | Largest number of name/value pairs         |
| `FORMS_MAX_FIELD_NAME_LENGTH`   | `128`    | Largest field name, in characters          |
| `FORMS_MAX_FIELD_VALUE_LENGTH`  | `16384`  | Largest field value, in characters         |

## Development

```bash
pytest                    # run the test suite
ruff check .              # lint
ruff format --check .     # formatting check
mypy                      # type check
```

### Layout

```
src/hymical_forms/
  app.py            application assembly
  config.py         typed settings
  errors.py         the shared JSON error envelope
  ingestion.py      domain rules: endpoint IDs, submission validation
  middleware.py     request body size limit
  main.py           ASGI entrypoint
  api/              HTTP routes and response models
```

`ingestion.py` holds the domain rules and knows nothing about HTTP; `api/`
translates requests into those rules and their outcomes into responses.

## Limitations

- **Nothing is stored.** A submission is validated, acknowledged, and dropped.
- **Nothing is delivered.** There are no webhooks, retries or delivery logs.
- **No authentication.** Any client can post to any syntactically valid endpoint
  ID, and there is no rate limiting or spam protection.
- **No file uploads.** Multipart text fields are accepted; file parts are
  rejected.
- **`multipart/form-data` bodies are buffered in memory,** bounded by
  `FORMS_MAX_BODY_BYTES`.
- Submission IDs are opaque and not yet guaranteed stable in format.

## License

[Apache License 2.0](LICENSE).

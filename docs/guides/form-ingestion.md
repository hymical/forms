# Form Ingestion

`POST /f/{endpoint_id}` accepts a form submission for a registered endpoint and
stores it. It is the URL that goes in the `action` attribute of an HTML form.

```html
<form action="https://forms.example.com/f/contact-form" method="POST">
  <input type="email" name="email" required />
  <textarea name="message"></textarea>
  <button type="submit">Send</button>
</form>
```

## This route is public and stays public

It cannot require a header a browser form has no way to send. No management
credential is read here, and one sent anyway is ignored rather than forwarded
anywhere.

Public does not mean unbounded. The route is rate limited per source address and
per endpoint, and an attempt over either limit is refused with
`429 rate_limit_exceeded`. See [Rate limiting](rate-limiting.md).

## Content types

`application/x-www-form-urlencoded` and `multipart/form-data` are both accepted,
so a plain HTML `<form>` works unchanged. Anything else is rejected with
`415 unsupported_media_type`.

File uploads are **not** supported. A multipart part carrying a file is rejected
with `422 file_upload_not_supported` rather than silently dropped, so you always
know a value did not make it.

## Repeated field names

Checkbox groups and multi-selects submit the same name several times. Every value
is preserved, in order, both in the response count and in storage:

```html
<input type="checkbox" name="topics" value="billing" checked />
<input type="checkbox" name="topics" value="api" checked />
```

is stored as `{"topics": ["billing", "api"]}`. No submitted value is discarded.
Every field is stored as a list, even when only one value arrived, so a webhook
receiver never has to guess whether a field is single or multi valued.

## What a successful submission returns

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

The status is `202 Accepted` and deliberately not `201 Created`: the submission
is stored and any delivery it owes is queued, but that delivery has not happened
yet.

Submitted values are not echoed back, because the client already has them.

`delivery.queued` says whether a webhook delivery is owed for this submission. No
outbound request is made during the form request, so the response says nothing
about whether your destination is reachable. That is the worker's business, and a
destination being down can no longer affect whether a form is accepted. See
[Webhook delivery](webhooks.md).

## Endpoints that do not accept the submission

| Situation | Response | Stored |
| --- | --- | --- |
| Endpoint ID is not well formed | `404 invalid_endpoint_id` | nothing |
| No endpoint with that ID exists | `404 endpoint_not_found` | nothing |
| Endpoint exists but `is_active` is false | `409 endpoint_inactive` | nothing |

Disabling an endpoint takes effect on the very next submission. Nothing caches
the endpoint, so there is no window in which a disabled endpoint still accepts a
form. See [Endpoint management](endpoint-management.md).

## Request limits

Every limit is configurable, and every one is enforced before anything is stored.

| Limit | Setting | Default | Refusal |
| --- | --- | --- | --- |
| Request body size | `FORMS_MAX_BODY_BYTES` | `262144` | `413 request_body_too_large` |
| Number of name/value pairs | `FORMS_MAX_FIELDS` | `100` | `422 too_many_fields` |
| Field name length | `FORMS_MAX_FIELD_NAME_LENGTH` | `128` | `422 field_name_too_long` |
| Field value length | `FORMS_MAX_FIELD_VALUE_LENGTH` | `16384` | `422 field_value_too_long` |

A repeated field name counts once per submitted value, so three checked boxes
under one name are three of your `FORMS_MAX_FIELDS` allowance.

Two content rules are not configurable:

- A field **name** may not be empty or contain control characters
  (`422 invalid_field_name`). Newlines inside a textarea **value** are fine.
- A field **value** may not contain a null byte (`422 invalid_field_value`).
- A submission with no fields at all is refused with `422 empty_submission`.

The body size cap runs in ASGI middleware, before the route and before the form
parser, so an oversized body is refused without being buffered. A request that
declares an oversized `Content-Length` is refused before a single body byte is
read, and a chunked request that omits `Content-Length` is cut off as soon as the
running total crosses the limit.

## Retrying safely

If a client never sees the response it cannot tell whether the submission landed.
Send an `Idempotency-Key` header and the retry becomes safe. See
[Idempotency](idempotency.md).

## Related

- [Submissions API reference](../api/submissions.md) for the compact route contract
- [Errors](../api/errors.md) for every code this route can return
- [Rate limiting](rate-limiting.md) for what counts as an attempt

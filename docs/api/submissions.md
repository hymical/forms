# Submissions API

The two public routes. Neither reads a credential.

For what ingestion accepts and why, see
[Form ingestion](../guides/form-ingestion.md).

## `POST /f/{endpoint_id}`

Accepts a form submission for a registered endpoint and stores it.

**Public.** This is the URL that goes in the `action` attribute of an HTML form,
so it cannot require a header a browser form has no way to send. A management
credential sent anyway is ignored rather than forwarded anywhere.

**Request**

| Part | Value |
| --- | --- |
| `Content-Type` | `application/x-www-form-urlencoded` or `multipart/form-data` |
| `Idempotency-Key` | Optional. 16 to 255 printable ASCII characters, no spaces |
| Body | Form fields. Repeated names are preserved in order |

**Success**

`202 Accepted`, not `201 Created`: the submission is stored and any delivery it
owes is queued, but that delivery has not happened yet.

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

| Field | Meaning |
| --- | --- |
| `submission_id` | Opaque identifier generated for this submission |
| `endpoint_id` | The endpoint the submission was addressed to |
| `received_at` | UTC timestamp of when the API accepted the body |
| `field_count` | Number of name/value pairs the submission carried |
| `idempotent_replay` | True when an earlier request already stored this submission |
| `delivery.queued` | True when a durable webhook delivery exists for this submission |

Submitted values are not echoed back.

**Responses**

| Status | Code | Cause |
| --- | --- | --- |
| `202` | | Accepted and stored |
| `400` | `malformed_form_body` | Body does not parse as the declared content type |
| `400` | `invalid_idempotency_key` | The header breaks the key format rules |
| `404` | `invalid_endpoint_id` | The path is not a well-formed endpoint ID |
| `404` | `endpoint_not_found` | No endpoint with that ID exists |
| `409` | `endpoint_inactive` | The endpoint is not accepting submissions |
| `409` | `idempotency_conflict` | The key was already used for different content |
| `413` | `request_body_too_large` | Body exceeded `FORMS_MAX_BODY_BYTES` |
| `415` | `unsupported_media_type` | Content type is not a supported form encoding |
| `422` | `file_upload_not_supported` | A multipart part carried a file |
| `422` | ingestion rule codes | See [Errors](errors.md) |
| `429` | `rate_limit_exceeded` | A rate limit was exhausted. Carries `Retry-After` |
| `503` | `storage_unavailable` | The database could not be reached |

Nothing is stored for any of the failure cases.

**Rate limiting.** This route is limited per source address and per endpoint.
Every request that reaches it spends budget, whether or not it is accepted. See
[Rate limiting](../guides/rate-limiting.md).

**Idempotency.** Send an `Idempotency-Key` to make a client retry safe. See
[Idempotency](../guides/idempotency.md).

## `GET /health`

Reports that the API process is running.

```json
{ "status": "ok", "service": "hymical-forms", "version": "0.1.0" }
```

**Public**, and not rate limited.

This is a liveness signal only. It does not check the database, so it stays
answerable while the database is down, which is what makes it useful for deciding
whether to restart the process. It is not a readiness check.

| Status | Cause |
| --- | --- |
| `200` | The process is running |

## Reading submissions back

**No public route returns a stored submission.** This one answers with an
acknowledgement, and the delivery views carry no submitted values either.

Reading a submission back is authenticated, on the routes in
[Submission Management](submission-management.md). See
[Data handling](../reference/data-handling.md) for everywhere a submitted value
does and does not go.

## Related

- [Form ingestion](../guides/form-ingestion.md) for content types, limits and repeated fields
- [Idempotency](../guides/idempotency.md)
- [Submission Management](submission-management.md) for reading submissions back
- [Errors](errors.md) for the full error table

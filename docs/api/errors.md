# Errors

Every non-2xx response uses one envelope. `code` is stable and machine-readable;
`details` appears only when there is something concrete to add.

```json
{
  "error": {
    "code": "too_many_fields",
    "message": "Submission carries 120 fields, which exceeds the limit of 100.",
    "details": { "limit": 100, "received": 120 }
  }
}
```

Nothing in the envelope exposes internal types, stack frames, file paths, table
names or SQL.

## Every code

| Status | `code` | Cause |
| --- | --- | --- |
| 400 | `malformed_form_body` | Body does not parse as the declared content type |
| 400 | `invalid_idempotency_key` | `Idempotency-Key` header breaks the key format rules |
| 401 | `authentication_required` | A management route was called without a bearer credential |
| 401 | `invalid_api_key` | The management API key is malformed, unknown or revoked |
| 404 | `invalid_endpoint_id` | Submission path is not a well-formed endpoint ID |
| 404 | `endpoint_not_found` | Endpoint ID is well formed but no such endpoint exists |
| 404 | `delivery_not_found` | No delivery with that ID exists |
| 404 | `not_found` | Unknown path |
| 405 | `method_not_allowed` | Wrong method for a known path |
| 409 | `endpoint_inactive` | Endpoint exists but is not accepting submissions |
| 409 | `endpoint_already_exists` | Endpoint ID is already taken |
| 409 | `idempotency_conflict` | Idempotency key already used for different content |
| 409 | `delivery_not_replayable` | Delivery has not terminally failed |
| 413 | `request_body_too_large` | Body exceeded `FORMS_MAX_BODY_BYTES` |
| 415 | `unsupported_media_type` | Content type is not a supported form encoding |
| 422 | `empty_submission` | No fields were submitted |
| 422 | `invalid_cursor` | Pagination cursor does not continue from a known row |
| 422 | `invalid_endpoint_id` | Endpoint ID in a request body breaks the ID rules |
| 422 | `invalid_request` | Request body or query parameters failed schema validation |
| 422 | `invalid_webhook_url` | Webhook destination is malformed or not permitted |
| 422 | `file_upload_not_supported` | A multipart part carried a file |
| 422 | ingestion rule codes | See below |
| 429 | `rate_limit_exceeded` | A public ingestion rate limit was exhausted |
| 500 | `internal_error` | Unexpected failure; no internals are exposed |
| 503 | `storage_unavailable` | The database could not be reached or written to |

## Ingestion rule codes

All `422`, all from [form ingestion](../guides/form-ingestion.md):

| `code` | Cause |
| --- | --- |
| `too_many_fields` | More name/value pairs than `FORMS_MAX_FIELDS` |
| `field_name_too_long` | A field name exceeds `FORMS_MAX_FIELD_NAME_LENGTH` |
| `field_value_too_long` | A field value exceeds `FORMS_MAX_FIELD_VALUE_LENGTH` |
| `invalid_field_name` | A field name is empty or contains control characters |
| `invalid_field_value` | A field value contains a null byte |

## Codes that need a note

### `invalid_endpoint_id` carries two statuses

`404` when the ID arrived as a submission path that addresses nothing, and `422`
when it arrived as a field in a request body. The same rule was broken; what
differs is whether the caller was addressing something or describing something.

### `endpoint_not_found` and `invalid_endpoint_id` share a status

Both are `404` on the submission route. From outside, both mean the path does not
address a form endpoint. The `code` tells the two apart.

### `rate_limit_exceeded` carries a header

It is the one error that returns `Retry-After`, in whole seconds. Its `details`
name which limit tripped (`scope` is `ip` or `endpoint`), the limit, and the
window length. See [Rate limiting](../guides/rate-limiting.md).

### Both `401`s carry `WWW-Authenticate: Bearer`

They are deliberately `401` and not `403`. A `403` says the caller is known and
not permitted, which needs a permission model this build does not have.

`invalid_api_key` is one answer for malformed, unknown and revoked credentials
alike, so that a guesser cannot sort their attempts into "nearly right" and
"wrong". The credential a request sent is never echoed back in an error.

### `storage_unavailable` rather than `500`

A database failure returns `503`, because the request itself was fine and
retrying it may well succeed. Driver messages carry table names, SQL text and
sometimes connection details, so none of the underlying exception reaches the
client.

### `internal_error` describes nothing

An unexpected failure returns an opaque `500`. That is deliberate: an error the
service did not anticipate is exactly the one whose details should not be
published.

## Related

- [Submissions](submissions.md), [Endpoints](endpoints.md) and [Deliveries](deliveries.md) for which codes each route can return
- [Authentication](authentication.md) for the `401` cases

# Submission Management API

Every route on this page requires a management API key. See
[Authentication](authentication.md).

These are the only routes that return what somebody typed into a form. Public
ingestion answers with an acknowledgement, and the delivery routes report
operational state. Reading a submission back is authenticated, always.

For what these routes are for, see
[Browsing submissions](../guides/submission-management.md) and
[Exporting submissions](../guides/exporting-submissions.md).

## `GET /submissions`

Lists stored submissions, newest first.

A listing is metadata. It reports how many values a submission carried, never
what they were, so walking a busy endpoint does not spread form content across
pages nobody asked for. Use the detail route or an export for the values.

**Query parameters**

| Parameter | Default | Meaning |
| --- | --- | --- |
| `endpoint_id` | none | Only submissions for one endpoint |
| `received_after` | none | Only submissions received **strictly after** this instant |
| `received_before` | none | Only submissions received **strictly before** this instant |
| `limit` | `50` | Page size, 1 to 100 |
| `cursor` | none | The previous page's `next_cursor` |

Both time bounds are ISO 8601 and both are exclusive: a submission received at
exactly the given instant is not returned by either. That is what lets you page
forward through a range by passing the last timestamp you saw back as
`received_after` without re-reading the row you took it from.

Paging works exactly as it does for endpoints. See
[Pagination](endpoints.md#pagination).

**Item fields**

| Field | Meaning |
| --- | --- |
| `id` | Submission identifier |
| `endpoint_id` | The endpoint the submission was addressed to |
| `received_at` | When the API accepted the body |
| `field_count` | Number of name/value pairs, counting a repeated field once per value |
| `idempotent` | Whether it was sent with an `Idempotency-Key` |
| `delivery` | The webhook delivery it owes, or `null` |

`delivery`, when present, carries `id`, `state` and `attempt_count`. A submission
owes at most one delivery.

**Responses**

| Status | Code | Cause |
| --- | --- | --- |
| `200` | | A page of submissions |
| `401` | `authentication_required`, `invalid_api_key` | Credential missing or unusable |
| `422` | `invalid_time_range` | `received_after` is not strictly earlier than `received_before` |
| `422` | `invalid_cursor` | The cursor does not continue from a known row |
| `422` | `invalid_request` | Unparseable timestamp, or `limit` outside 1 to 100 |
| `503` | `storage_unavailable` | The database could not be reached |

A filter that matches nothing is not an error. It is a filter that selected no
rows, and the answer is an empty page. A range that *cannot* match anything, such
as `received_after` on or later than `received_before`, is refused instead:
that is a mistake in the request rather than a fact about the data.

## `GET /submissions/{submission_id}`

One submission, including the values it carried.

**Fields**

Everything a listing item carries, plus:

| Field | Meaning |
| --- | --- |
| `fields` | The submitted field names and their ordered values |

`fields` is exactly what was stored, and every value is a list:

```json
{
  "email": ["dev@example.com"],
  "topics": ["billing", "api"]
}
```

A field submitted once is a one-element list rather than a bare string, and a
repeated field keeps its values in the order they were sent. That is the same
shape the signed webhook payload uses, so a receiver and an operator see the same
thing.

**Responses**

| Status | Code | Cause |
| --- | --- | --- |
| `200` | | The submission and its fields |
| `401` | `authentication_required`, `invalid_api_key` | Credential missing or unusable |
| `404` | `submission_not_found` | No submission with that ID exists |
| `503` | `storage_unavailable` | The database could not be reached |

A submission that [retention](../operations/retention.md) has deleted answers
`404`, the same as one that never existed. From outside, both mean this service
does not hold it.

!!! note "What is never in these responses"

    The payload fingerprint, the `Idempotency-Key` the submission was sent with,
    any webhook signing secret, and any management credential. The fingerprint is
    an internal detail of how a retry is recognised. The idempotency key is a
    secret in practice: anyone holding it can resolve it to a submission through
    the public ingestion route.

## `GET /submissions/export`

Exports the submissions a filter matches, as a downloadable file.

**Query parameters**

The same `endpoint_id`, `received_after` and `received_before` as the listing,
with the same exclusive bounds, plus:

| Parameter | Default | Meaning |
| --- | --- | --- |
| `format` | `json` | `json` or `csv` |

There is no `limit` and no `cursor`. An export is the whole filtered set or an
error, never a page.

**Responses**

| Status | Code | Cause |
| --- | --- | --- |
| `200` | | The export, as an attachment |
| `401` | `authentication_required`, `invalid_api_key` | Credential missing or unusable |
| `422` | `export_too_large` | The filter matches more than `FORMS_EXPORT_MAX_SUBMISSIONS` |
| `422` | `unsupported_export_format` | `format` is not `json` or `csv` |
| `422` | `invalid_time_range` | `received_after` is not strictly earlier than `received_before` |
| `503` | `storage_unavailable` | The database could not be reached |

Both formats are sent with `Content-Disposition: attachment` and a generated
filename such as `hymical-submissions-2026-08-25.json`. Nothing a caller supplied
reaches the filename.

The format and the size limit are described in
[Exporting submissions](../guides/exporting-submissions.md).

## Related

- [Browsing submissions](../guides/submission-management.md)
- [Exporting submissions](../guides/exporting-submissions.md)
- [Retention](../operations/retention.md)
- [Data handling](../reference/data-handling.md)
- [Errors](errors.md) for the full error table

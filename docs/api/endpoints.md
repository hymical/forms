# Endpoints API

Every route on this page requires a management API key. See
[Authentication](authentication.md).

For what these routes *mean*, including secret rotation and active-state
behaviour, see [Endpoint management](../guides/endpoint-management.md).

## `POST /endpoints`

Registers a form endpoint.

**Request body**

| Field | Required | Type | Meaning |
| --- | --- | --- | --- |
| `id` | yes | string | The public identifier the endpoint answers on |
| `name` | yes | string | Human-readable label, 1 to 200 characters |
| `is_active` | no | boolean | Whether it accepts submissions, defaults to `true` |
| `webhook_url` | no | string | Where accepted submissions are delivered |

`id` is 3 to 64 characters of lowercase ASCII letters, digits, `-` and `_`, and
must start and end with a letter or digit.

**Responses**

| Status | Code | Cause |
| --- | --- | --- |
| `201` | | Created |
| `401` | `authentication_required`, `invalid_api_key` | Credential missing or unusable |
| `409` | `endpoint_already_exists` | The ID is already taken |
| `422` | `invalid_endpoint_id` | The ID breaks the format rules |
| `422` | `invalid_webhook_url` | The destination is malformed or not permitted |
| `422` | `invalid_request` | The body failed schema validation |
| `503` | `storage_unavailable` | The database could not be reached |

`webhook_secret` is present in the `201` body only when a secret was generated.
It is never returned again.

## `GET /endpoints`

Lists endpoints, newest first.

**Query parameters**

| Parameter | Default | Meaning |
| --- | --- | --- |
| `limit` | `50` | Page size, 1 to 100 |
| `cursor` | none | The previous page's `next_cursor` |

**Responses**

| Status | Code | Cause |
| --- | --- | --- |
| `200` | | A page of endpoints |
| `401` | `authentication_required`, `invalid_api_key` | Credential missing or unusable |
| `422` | `invalid_cursor` | The cursor does not continue from a known row |
| `422` | `invalid_request` | `limit` is outside 1 to 100 |
| `503` | `storage_unavailable` | The database could not be reached |

## `GET /endpoints/{endpoint_id}`

One endpoint, in the same representation as one item of the listing.

**Responses**

| Status | Code | Cause |
| --- | --- | --- |
| `200` | | The endpoint |
| `401` | `authentication_required`, `invalid_api_key` | Credential missing or unusable |
| `404` | `endpoint_not_found` | No endpoint with that ID exists |
| `503` | `storage_unavailable` | The database could not be reached |

## `PATCH /endpoints/{endpoint_id}`

Changes an endpoint. Send only what should change; anything omitted is left
exactly as it was.

**Request body**

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | New label, 1 to 200 characters |
| `is_active` | boolean | Whether the endpoint accepts submissions |
| `webhook_url` | string or `null` | New destination, or `null` to remove the webhook |

An `id` in the body is ignored. The endpoint ID is the primary key and cannot be
changed.

**Responses**

| Status | Code | Cause |
| --- | --- | --- |
| `200` | | The changed endpoint |
| `401` | `authentication_required`, `invalid_api_key` | Credential missing or unusable |
| `404` | `endpoint_not_found` | No endpoint with that ID exists |
| `422` | `invalid_webhook_url` | The destination is malformed or not permitted |
| `422` | `invalid_request` | The body failed schema validation |
| `503` | `storage_unavailable` | The database could not be reached |

Changing `webhook_url` to a different value generates a new signing secret and
returns it in this response. See
[Changing the webhook](../guides/endpoint-management.md#changing-the-webhook).

## Pagination

Both management list routes, `GET /endpoints` and
[`GET /deliveries`](deliveries.md), page the same way.

Items come back newest first, ordered by creation time with the identifier as a
tie-break, so a page is exactly reproducible. `limit` is 1 to 100 and defaults to
50; anything outside that is refused with `422 invalid_request` rather than
quietly clamped. A caller that asked for a thousand rows and silently received a
hundred would page wrongly and never find out.

`next_cursor` is the identifier of the last item on the page. Pass it as `cursor`
to continue:

```bash
curl "http://127.0.0.1:8000/deliveries?limit=50&cursor=whd_9a3f..." \
  -H "Authorization: Bearer $HYMICAL_KEY"
```

A full page always carries a cursor, even when it happens to be the last one,
because knowing otherwise would cost an extra read on every page. **A walk
therefore ends on one empty page rather than on a null cursor.**

A cursor that names no row is refused with `422 invalid_cursor` rather than
treated as the first page, so a caller that pages past a row somebody deleted
learns about it instead of silently starting again from the top.

There is deliberately **no total count**. Counting an operational table on every
page is not free, and nothing here needs the number.

## Related

- [Endpoint management](../guides/endpoint-management.md) for the behaviour behind these routes
- [Errors](errors.md) for the full error table

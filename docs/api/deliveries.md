# Deliveries API

Every route on this page requires a management API key. See
[Authentication](authentication.md).

For what these routes mean, including the retry-cycle model and concurrent
replay, see [Delivery inspection and replay](../guides/delivery-replay.md).

## `GET /deliveries`

Lists the delivery queue, newest first.

**Query parameters**

| Parameter | Default | Meaning |
| --- | --- | --- |
| `endpoint_id` | none | Only deliveries for one endpoint |
| `state` | none | One of `pending`, `processing`, `delivered`, `failed` |
| `limit` | `50` | Page size, 1 to 100 |
| `cursor` | none | The previous page's `next_cursor` |

Paging works exactly as it does for endpoints. See
[Pagination](endpoints.md#pagination).

**Item fields**

| Field | Meaning |
| --- | --- |
| `id` | Delivery identifier |
| `submission_id` | The submission this delivery carries |
| `endpoint_id` | The endpoint that submission was addressed to |
| `state` | `pending`, `processing`, `delivered` or `failed` |
| `destination_url` | The URL snapshotted when the submission was accepted |
| `attempt_count` | Every request ever made for this delivery |
| `cycle_attempt_count` | Requests made since it last entered the queue |
| `next_attempt_at` | When it next becomes due |
| `created_at` | When the delivery was queued |
| `completed_at` | When it reached a terminal state, or `null` |

**Responses**

| Status | Code | Cause |
| --- | --- | --- |
| `200` | | A page of deliveries |
| `401` | `authentication_required`, `invalid_api_key` | Credential missing or unusable |
| `422` | `invalid_cursor` | The cursor does not continue from a known row |
| `422` | `invalid_request` | Unknown `state`, or `limit` outside 1 to 100 |
| `503` | `storage_unavailable` | The database could not be reached |

An `endpoint_id` that matches nothing is not an error. It is a filter that
selected no rows, and the answer is an empty page.

## `GET /deliveries/{delivery_id}`

One delivery, with its ordered attempt history.

**Attempt fields**

| Field | Meaning |
| --- | --- |
| `attempt_number` | Position in the lifetime history, never reused |
| `attempted_at` | When the request went out |
| `outcome` | `succeeded`, `http_error`, `timeout` or `network_error` |
| `response_status` | The HTTP status, or `null` if the destination never answered |
| `error` | A bounded failure message, or `null` |

Attempts are ordered by `attempt_number`, ascending.

**Responses**

| Status | Code | Cause |
| --- | --- | --- |
| `200` | | The delivery and its attempts |
| `401` | `authentication_required`, `invalid_api_key` | Credential missing or unusable |
| `404` | `delivery_not_found` | No delivery with that ID exists |
| `503` | `storage_unavailable` | The database could not be reached |

!!! note "What is never in these responses"

    Submitted field values, the snapshotted signing secret, the request headers,
    and the response body. The first three are never returned by any route; the
    last is never stored.

## `POST /deliveries/{delivery_id}/replay`

Puts a terminally failed delivery back in the queue.

Takes no request body.

**Responses**

| Status | Code | Cause |
| --- | --- | --- |
| `200` | | Requeued. The body is the delivery's new state |
| `401` | `authentication_required`, `invalid_api_key` | Credential missing or unusable |
| `404` | `delivery_not_found` | No delivery with that ID exists |
| `409` | `delivery_not_replayable` | The delivery is not in terminal `failed` |
| `503` | `storage_unavailable` | The database could not be reached |

`200`, not `202`: this request finishes here, and what it leaves behind is a
queued row. The API process sends nothing.

A replay resets `cycle_attempt_count` to zero, granting a fresh retry schedule,
and leaves `attempt_count` and every historical attempt row untouched.

## Related

- [Delivery inspection and replay](../guides/delivery-replay.md)
- [Webhook delivery](../guides/webhooks.md) for the retry schedule
- [Errors](errors.md) for the full error table

# Idempotency

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

!!! note "The server does not retry anything on your behalf"

    It only makes *your* retries safe. Nothing here re-sends a form for you.

## Retry semantics

Repeating the same key on the same endpoint with the same content returns `202`
with the **original** `submission_id` and `received_at`, and
`"idempotent_replay": true`:

```json
{
  "submission_id": "sub_48984534f33749c49a88de2d59400dce",
  "endpoint_id": "contact-form",
  "received_at": "2026-08-24T14:34:27.651841Z",
  "field_count": 2,
  "idempotent_replay": true,
  "delivery": { "queued": true }
}
```

Only one row is ever stored, and this holds even when the retries arrive at the
same instant: a unique constraint on `(endpoint_id, idempotency_key)` decides the
winner, not a check in application code. See
[Concurrency](../architecture/concurrency.md).

A replay reports the same `delivery.queued` as the original and does **not**
queue a second delivery. One submission owes at most one delivery, enforced by a
unique constraint on the submission.

## Conflict semantics

Reusing a key on the same endpoint with **different** content is rejected with
`409 idempotency_conflict`. The stored submission is left exactly as it was, and
the response never describes its contents.

```json
{
  "error": {
    "code": "idempotency_conflict",
    "message": "The Idempotency-Key '...' was already used on endpoint 'contact-form' for a different submission.",
    "details": { "endpoint_id": "contact-form", "idempotency_key": "..." }
  }
}
```

## How content is compared

By a SHA-256 fingerprint of the normalized fields. Because field order and
repeated values are meaningful to this service, they are meaningful to the
fingerprint too:

- `a=1&b=2` and `b=2&a=1` are **different** payloads and will conflict.
- `topics=api&topics=billing` and `topics=billing&topics=api` are different too.

The generated submission ID and the received timestamp are excluded from the
fingerprint, so an honest retry always matches.

## Scope

A key belongs to **one endpoint**. The same key may be used once per endpoint
without conflicting.

A key has no expiry of its own: it is spent for as long as its submission is
stored, because the uniqueness constraint lives on that row. Configuring
[retention](../operations/retention.md) is therefore what eventually releases
one, and without it the table only grows. Retention ages are far longer than any
client's retry window, so this is bookkeeping rather than a behaviour change.

## Key format

16 to 255 printable ASCII characters with no spaces, which accepts UUIDs, hex,
base64 and base64url. Anything else is rejected with
`400 invalid_idempotency_key`, including a header that is present but empty. An
empty header is a client bug, and treating it as absent would silently drop the
guarantee the client was asking for.

The 16-character floor exists because keys are endpoint-scoped and form ingestion
is public, so every client of an endpoint shares one key space. A short or
predictable key would collide with a stranger's submission. **Use a random
value.** Management API keys do not change this: they authenticate the endpoint's
administrator, not the visitors submitting the form.

## Interaction with rate limiting

**An idempotent replay counts against both rate limits.** A repeated key is still
a request that crosses the network and reaches the database, and exempting it
would make one leaked key an unlimited way past the limits.

A replay the limits allow behaves exactly as described above. A replay the limits
refuse gets `429` and changes nothing: the stored submission and its delivery are
untouched. See [Rate limiting](rate-limiting.md).

## What a replay does not do

A replay is only recognised once the first attempt has **committed**. A retry
sent while the original is still in flight is treated as a concurrent request,
which is safe and settles on one row. A retry sent after the original *failed* is
a new submission, which is correct: nothing was stored the first time.

## Related

- [Form ingestion](form-ingestion.md) for what ingestion accepts
- [Submissions API reference](../api/submissions.md)
- [Concurrency](../architecture/concurrency.md) for how the race is settled

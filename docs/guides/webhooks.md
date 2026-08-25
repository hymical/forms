# Webhook Delivery

If an endpoint has a `webhook_url`, accepting a submission also writes a durable
delivery record in the **same transaction**. Nothing is sent during the form
request. A worker picks the delivery up, sends it, and retries it if it fails.

That transaction is the whole reliability claim. Once `POST /f/{endpoint_id}`
answers `202`, the submission is stored *and* the obligation to deliver it is
stored. A crash at any point after that cannot lose the delivery, because it is a
row rather than a thing the API process was about to do. See
[Transactional outbox](../architecture/transactional-outbox.md).

## Payload

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

## Verifying the signature

Each request carries a `Hymical-Signature` header:

```
Hymical-Signature: v1=9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08
```

The digest is HMAC-SHA256 of the **raw request body**, keyed with the endpoint's
`webhook_secret`.

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

!!! warning "Verify the exact bytes you received"

    Verify before parsing the JSON. Re-serializing the payload will produce
    different bytes and the signature will fail. Use a constant-time comparison,
    as `hmac.compare_digest` does above.

The `v1=` prefix exists so a future scheme can be added without breaking
receivers that only understand this one.

## Deliveries snapshot their destination

A delivery keeps the destination URL and signing secret that were configured when
the submission was accepted. Changing an endpoint's webhook does not redirect
deliveries that are already queued, and does not leave a queued payload signed
with a secret its receiver never had. See
[Endpoint management](endpoint-management.md).

## Delivery states

| State | Meaning |
| --- | --- |
| `pending` | Owed, waiting for its next attempt time |
| `processing` | Claimed by a worker, holding a lease |
| `delivered` | A destination answered `2xx`. Terminal |
| `failed` | Given up on. Terminal |

## What retries, and what does not

| Outcome | Retried |
| --- | --- |
| Connection failure | yes |
| Timeout | yes |
| HTTP `5xx` | yes |
| HTTP `408`, `425`, `429` | yes |
| HTTP `2xx` | delivered |
| Any other `4xx`, including `409` | no |
| `3xx` | no |

Ordinary `4xx` responses are final because repeating a request the receiver
called malformed, unauthorized or missing will not repair it. `409` is treated as
final too: from a webhook receiver it almost always means "I already have this
event". Redirects are not followed, and a `3xx` is a misconfiguration rather than
a passing problem, so it is final as well.

## Retry schedule

Delivery is attempted immediately, then backs off by doubling, capped:

| Attempt | Waits before it |
| --- | --- |
| 1 | none |
| 2 | 10s |
| 3 | 20s |
| 4 | 40s |
| 5 | 80s |

After `FORMS_WEBHOOK_MAX_ATTEMPTS` (default 5) the delivery becomes `failed` and
is never retried automatically. There is no jitter: the schedule is deliberately
exactly predictable. Every attempt, including the last, stays in the attempt
history.

The waits are set by `FORMS_WEBHOOK_RETRY_INITIAL_SECONDS` (default 10) doubling
each time, capped at `FORMS_WEBHOOK_RETRY_MAX_SECONDS` (default 3600).

The allowance is per retry **cycle**, not per lifetime, so a
[manual replay](delivery-replay.md) starts the schedule again from the top.

## At-least-once, not exactly-once

A worker claims a delivery by taking a lease on it. If the worker dies, the lease
expires and another worker picks the delivery up, which is what stops a crash
from stranding work in `processing` forever.

!!! danger "Duplicate delivery is possible"

    A worker that sends successfully and then dies before recording that success
    leaves a delivery that looks unsent, and the next worker sends it again. No
    queue can close this window on its own; it needs the receiver's cooperation.

    **Use the `id` in the signed payload to ignore an event you have already
    processed.**

Hymical Forms does not offer exactly-once delivery and there is no message broker
involved: PostgreSQL is the queue. See
[Delivery semantics](../architecture/delivery-semantics.md).

## What is recorded

`webhook_deliveries` holds one row per logical delivery: its state, how many
attempts it has had, when it is next due, and when it finished.

`delivery_attempts` holds one row per request that actually went out, numbered,
with the outcome, the HTTP status when there was one, and a bounded failure
message. Response bodies are **not** stored, and neither is the signing secret. A
job that is inspected and found not due records nothing.

Both are readable through [Delivery replay](delivery-replay.md).

## Destinations that are refused

A webhook URL must use `http` or `https` and must not name a loopback, private,
link-local, multicast, reserved or unspecified address. That covers `localhost`,
`127.0.0.1`, `[::1]`, `[::ffff:127.0.0.1]`, `10.0.0.0/8`, `192.168.0.0/16`, and
the `169.254.169.254` cloud metadata endpoint.

Rejections return `422 invalid_webhook_url`. The same check runs on a destination
supplied through `PATCH /endpoints/{endpoint_id}`.

!!! warning "This is not complete SSRF protection"

    Hostnames are **not** resolved, so a name that resolves to a private address
    still passes, and DNS rebinding is not addressed at all. Treat these checks
    as a guardrail against mistakes, not a defence against an attacker who holds
    a management key. See [Security](../architecture/security.md).

For local development, `FORMS_ALLOW_PRIVATE_WEBHOOK_TARGETS=true` lifts the
address restriction so you can point a webhook at a server on your own machine.
Do not enable it in production.

## Timeouts

| Setting | Default | Meaning |
| --- | --- | --- |
| `FORMS_WEBHOOK_CONNECT_TIMEOUT_SECONDS` | `5` | Wait for the destination to accept a connection |
| `FORMS_WEBHOOK_READ_TIMEOUT_SECONDS` | `10` | Wait for the destination to respond |

Both must stay comfortably below `FORMS_WORKER_LEASE_SECONDS`, or another worker
could claim a delivery that is still in flight. See [Worker](../operations/worker.md).

## Related

- [Deliveries API reference](../api/deliveries.md)
- [Delivery replay](delivery-replay.md) for requeueing a failed delivery
- [Worker](../operations/worker.md) for running and tuning the delivery process

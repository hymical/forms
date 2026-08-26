# Delivery Semantics

Hymical Forms delivers **at least once**. It does not offer exactly-once, and no
configuration makes it do so.

This page explains why, and what that means for the code on the other end.

## Leases

A worker takes ownership of a delivery by claiming it and setting a lease
expiry. While the lease holds, no other worker will touch the row.

```mermaid
flowchart LR
    A[pending, due] -->|worker claims| B["processing, lease expires at T"]
    B -->|success| C[delivered]
    B -->|retryable failure| A
    B -->|allowance exhausted| D[failed]
    B -->|"worker dies, T passes"| A
```

A delivery is claimable when it is `pending` and due, **or** when it is
`processing` and its lease has expired. The second case is the whole point: a
worker that dies holding a job does not strand it in `processing` forever.

## Who owns a claim

A lease says *when* a claim ends. It does not say *which* claim the row is
holding, and that is a different question with a different answer.

Every claim mints a fresh `claim_token` onto the delivery. A worker records its
result against the token it claimed under, and that update is conditional on the
row still carrying it:

```sql
UPDATE webhook_deliveries
SET state = ..., cycle_attempts = ..., claim_expires_at = NULL, claim_token = NULL
WHERE id = :id AND claim_token = :the_token_this_request_was_sent_under
```

This matters because a worker whose lease ran out mid-request has no way of
noticing on its own. It is still looking at a row that says `processing`, so
neither the state nor the lease tells it anything. The token does: once another
worker has reclaimed the delivery, the token no longer matches, and the late
worker's transition matches no row.

So a superseded worker **cannot**:

- clear or shorten the current owner's lease
- move the delivery to `delivered`, `failed` or `pending`
- spend an attempt from the current owner's retry cycle
- move `next_attempt_at`

What it still does is record the request it genuinely made. See
[what happens to a late attempt](#what-happens-to-a-late-attempt) below.

**Tested against real PostgreSQL** with independent connections holding two
different claims on one row. See [Testing](../development/testing.md).

!!! warning "Every worker has to be on the same build"

    This holds only while every running worker maintains the token. A worker from
    a build older than the `claim_token` migration neither writes it when it
    claims nor checks it when it completes, so one left running alongside a newer
    worker defeats the fence. Stop the old workers, migrate, then start the new
    ones, which is the [deploy order](../operations/worker.md#deploy-order) this
    project documents anyway.

## The crash window

Here is the sequence that produces a duplicate:

1. Worker claims the delivery. State is `processing`.
2. Worker sends the HTTP request. Your server receives it and returns `200`.
3. Worker dies before it can write that outcome.
4. The lease expires. The delivery still says `processing`.
5. Another worker claims it and sends the same event again.

!!! danger "This window cannot be closed from this side"

    Between "the request was received" and "the outcome was recorded" there is
    always a moment where the process can die. Making the send and the record
    atomic would require a distributed transaction with your server, which is not
    something a webhook receiver offers.

No queue closes this on its own. It needs the receiver's cooperation.

Note what the claim token does and does not change here. It stops a late worker
from overwriting a newer worker's state. It does **not** stop the request from
having been sent twice: by the time ownership is checked, both requests have
already left. Your receiver still has to deduplicate.

## What you should do about it

**Deduplicate on the submission `id` in the signed payload.**

```json
{
  "type": "submission.received",
  "submission": {
    "id": "sub_48984534f33749c49a88de2d59400dce",
    "...": "..."
  }
}
```

That identifier is stable across every attempt and every replay of the same
logical delivery. Record the ones you have processed and ignore repeats.

Making your handler idempotent is worth doing regardless: it is also what makes a
[manual replay](../guides/delivery-replay.md) safe to run.

## Shortening the window

Lowering `FORMS_WORKER_LEASE_SECONDS` makes recovery from a dead worker faster.
It does not remove the window, and set too low it **creates** duplicates:
another worker can claim a delivery that is still in flight.

The lease must comfortably outlast `FORMS_WEBHOOK_CONNECT_TIMEOUT_SECONDS` and
`FORMS_WEBHOOK_READ_TIMEOUT_SECONDS` combined. The defaults leave a wide margin,
60 seconds against 15. See [Worker](../operations/worker.md).

## Two attempt counters

A delivery carries two counts, because they answer two different questions:

| Column | Question |
| --- | --- |
| `attempts` | How many requests has this delivery ever produced? |
| `cycle_attempts` | How many since it last entered the queue? |

`attempts` only ever rises, so it can number the audit trail without a number
ever being reused, however often a delivery is replayed.

`cycle_attempts` is what the retry allowance is measured against, and a replay
resets it to zero. That is what lets a replay grant a whole fresh schedule rather
than one last attempt against an allowance that is already spent.

The two numbers start out equal, which is why the migration that introduced the
second one backfilled it from the first. They diverge for one of two reasons: the
delivery was replayed, or a worker that had lost its claim recorded a request it
really made. Only the current owner may draw on the retry allowance, so a late
attempt raises `attempts` and leaves `cycle_attempts` alone.

## What happens to a late attempt

A worker whose claim was superseded has usually already made a real HTTP request.
Somebody's server received it. Discarding that would make the attempt history
claim fewer requests went out than actually did, so it is recorded:

- it takes the **next free lifetime attempt number**, read from the stored row
  rather than from whatever the worker held in memory, so no number is reused
- it raises `attempts`, because the delivery really did produce that request
- it changes **nothing else**

The number is taken under a row lock, so a late worker and the current owner
recording at the same instant still get two different numbers.

## Terminal is terminal, until an operator says otherwise

`delivered` and `failed` are terminal states, and the worker never revisits
either. A `failed` delivery is not retried on its own and nothing notices it for
you: there is no alerting, no dead-letter notification and no automatic sweep.

An operator can put it back in the queue with
[manual replay](../guides/delivery-replay.md). That is a deliberate action, not a
background process, because a delivery that exhausted five attempts usually
failed for a reason that needs fixing first.

## What is recorded, and what is not

| Recorded | Not recorded |
| --- | --- |
| One row per logical delivery: state, counts, due time, completion | The response body from your server |
| One row per request that actually went out, numbered | The signing secret, in the attempt history |
| Outcome, HTTP status when there was one, bounded error text | Request headers |

A job that is inspected and found not due records nothing. Response bodies are
unbounded, written by somebody else's server, and nothing in this build reads
them back, so they are not stored at all.

## Related

- [Transactional outbox](transactional-outbox.md) for how the delivery got there
- [Concurrency](concurrency.md) for how two workers avoid the same row
- [Webhook delivery](../guides/webhooks.md) for the retry schedule and signing

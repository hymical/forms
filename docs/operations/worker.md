# Worker

Hymical Forms is two processes sharing one database.

```bash
uvicorn hymical_forms.main:app
```

```bash
python -m hymical_forms.worker
```

The **API** accepts submissions, stores them, and records that a webhook is owed.
It never makes an outbound request. The **worker** claims owed deliveries, sends
them, and retries the ones that fail.

Running the API alone is fine: submissions are still accepted and nothing is
lost, they simply wait until a worker exists.

## What one poll does

1. Claim up to `FORMS_WORKER_BATCH_SIZE` deliveries that are due, taking a lease
   on each.
2. Send them, concurrently.
3. Record each attempt and move each delivery to whatever it earned: `delivered`,
   `pending` with a later due time, or `failed`.
4. Wait `FORMS_WORKER_POLL_SECONDS` and go again.

A delivery is due when it is `pending` and its `next_attempt_at` has passed, or
when it is `processing` and its lease has expired. The second case is how work is
recovered from a worker that died holding it.

## Running more than one

Workers claim with `SELECT ... FOR UPDATE SKIP LOCKED` on PostgreSQL, so two
workers scanning at once are handed different rows rather than fighting over the
same one. You can run as many as you like without configuring anything.

There is no coordination service and no broker. PostgreSQL is the queue. See
[Concurrency](../architecture/concurrency.md).

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `FORMS_WORKER_POLL_SECONDS` | `1` | How long an idle worker waits before looking again |
| `FORMS_WORKER_BATCH_SIZE` | `10` | How many deliveries a worker claims at once |
| `FORMS_WORKER_LEASE_SECONDS` | `60` | How long a worker's claim on a delivery holds |

!!! danger "The lease must outlast a delivery attempt"

    A batch is delivered concurrently, so it takes about as long as its slowest
    single delivery rather than the sum. But if `FORMS_WORKER_LEASE_SECONDS` were
    set below `FORMS_WEBHOOK_CONNECT_TIMEOUT_SECONDS` and
    `FORMS_WEBHOOK_READ_TIMEOUT_SECONDS` combined, another worker could claim a
    delivery that is still in flight and send it twice.

    The defaults leave a wide margin (60 seconds against 15). Keep it that way if
    you change them.

    A lease that runs out mid-request costs you a duplicate send, not a corrupted
    delivery: the overtaken worker cannot overwrite the state of whoever reclaimed
    the delivery. It logs a warning saying so, which is the signal that the lease
    is too short for how long that destination takes to answer. See
    [Delivery semantics](../architecture/delivery-semantics.md#who-owns-a-claim).

## Startup checks

The worker performs the same schema check the API does. It reaches the database,
confirms the schema is the revision this build was written against, and refuses
to serve if it is not:

```
the database is at migration '0003' but this build expects '0004'.
Run 'alembic upgrade head' before starting.
```

Migrating is an operator action, run when the operator chooses. See
[Database migrations](migrations.md).

## Deploy order

Stop the old processes, migrate, start the new ones. Migrating while an old build
is still running is only safe if the change happens to be backwards compatible,
and this project does not promise that for any particular migration.

## What the worker does not do

- **It does not authenticate through the HTTP API.** It shares only the database
  with the API process.
- **It does not sweep failed deliveries.** A delivery that exhausted its retry
  allowance stays `failed` until an operator replays it. There is no alerting and
  no dead-letter notification. See [Delivery replay](../guides/delivery-replay.md).
- **It does not follow redirects.** A `3xx` from a destination is treated as a
  misconfiguration and is final.

## Related

- [Webhook delivery](../guides/webhooks.md) for the retry schedule and signing
- [Delivery semantics](../architecture/delivery-semantics.md) for the at-least-once model
- [Configuration reference](configuration-reference.md)

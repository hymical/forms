# Concurrency

Four places in this service have a race, and every one of them is settled by the
database rather than by a check in Python. The first has two halves: taking a
delivery, and recording what happened to it.

The pattern is the same every time: **read, compare in application code, then
write** is never used, because two requests can both read, both find room, and
both write.

## 1. Two workers claiming the same delivery

Workers claim with `SELECT ... FOR UPDATE SKIP LOCKED`:

```sql
SELECT * FROM webhook_deliveries
WHERE <due>
ORDER BY next_attempt_at
LIMIT 10
FOR UPDATE SKIP LOCKED
```

PostgreSQL hands each worker a different set of rows outright. `SKIP LOCKED`
means a row another worker holds is passed over rather than waited on, so workers
proceed in parallel instead of funnelling into one.

The claim also performs a conditional `UPDATE` and treats a row as claimed only
if that update matched. Under `SKIP LOCKED` that guard is redundant. It is there
because SQLite silently ignores `FOR UPDATE`, and the conditional update is what
keeps the claim correct there.

**Tested against real PostgreSQL:** six sessions claiming at once partition the
work and never share it; a locked row is skipped rather than waited on; an
expired lease is reclaimed by exactly one of two racing workers.

### Recording a result under a claim that was superseded

Claiming and recording are separated by an HTTP request to somebody else's
server, which can take longer than the lease. So the second half of the claim has
a race of its own: worker A's lease expires, worker B legitimately reclaims the
delivery, and *then* A's request comes back.

Each claim mints a `claim_token`, and the transition that records a result is
conditional on the row still carrying it:

```sql
UPDATE webhook_deliveries
SET state = ..., cycle_attempts = ..., claim_expires_at = NULL, claim_token = NULL
WHERE id = :id AND claim_token = :token
```

The lease expiry cannot do this job. It says when a claim ends, not which claim it
is, and A is still looking at a `processing` row, so neither the state nor the
lease tells A that anything changed. A's update matches no row, and B's state
stands.

The request A made is still recorded, taking the next free lifetime attempt
number read under a row lock. See
[Delivery semantics](delivery-semantics.md#what-happens-to-a-late-attempt).

**Tested against real PostgreSQL** with two connections holding two different
claims on one row, in both result orderings, so what the fence keys on is
ownership rather than whether the late worker happened to succeed.

## 2. Two retries with the same idempotency key

A unique constraint on `(endpoint_id, idempotency_key)` is the authority.

The lookup before inserting is an **optimisation** for the ordinary retry, never
the guarantee. When two requests race, both find nothing, both try to insert, and
one loses on the constraint. The loser rolls back, reads the winner's row, and
answers with it.

The rollback is mandatory rather than tidy: a session holding a failed flush
refuses every later query, so the read that finds the winner would fail without
it. Rolling back also discards the loser's submission and its delivery together,
leaving exactly the one submission and the one delivery the winner committed.

Both PostgreSQL and SQLite treat NULLs in a unique constraint as distinct from
each other, so submissions sent without a key stay unrestricted without needing a
partial index on either backend.

## 3. Two operators replaying the same delivery

One conditional `UPDATE`:

```sql
UPDATE webhook_deliveries
SET state = 'pending', cycle_attempts = 0, ...
WHERE id = :id AND state = 'failed'
```

Whoever gets there first flips the row out of `failed`, and the loser's update
matches no row. Both then read back the state the database actually settled on,
so the loser is refused with `409 delivery_not_replayable` rather than resetting
the retry cycle a second time.

Reading the state, judging it in Python and then writing would let both requests
pass the check and both reset the cycle, which is exactly the duplicated work
this exists to prevent.

**Tested against real PostgreSQL** with independent connections.

## 4. Two submissions hitting the same rate limit

One statement does the increment and the read:

```sql
INSERT INTO rate_limit_counters (limiter, subject, window_start, attempts)
VALUES (:limiter, :subject, :window, 1)
ON CONFLICT (limiter, subject, window_start)
DO UPDATE SET attempts = rate_limit_counters.attempts + 1
RETURNING attempts
```

The database increments under its own row lock and hands back the value it
settled on, so two simultaneous requests receive two different numbers and at
most one of them can be the last one under the limit.

An upsert rather than a lock-then-update, because the row for a brand new subject
does not exist yet and two requests racing to create it have nothing to lock. The
upsert makes the create and the increment the same operation, settled by the
primary key.

**Tested against real PostgreSQL** with several independently built applications,
each with its own engine and connection pool, submitting at the same instant.
Exactly the configured number of attempts is accepted, no increment is lost, and
a budget one application spent is already spent for another that has never seen
the client before. That last one is what "shared enforcement rather than
process-local state" has to mean.

## Endpoint updates are last-write-wins

`PATCH /endpoints/{endpoint_id}` reads the row `FOR UPDATE` on PostgreSQL, so two
operators changing one endpoint's webhook at the same moment are serialised and
both get an answer that matches what was stored.

Without the lock, both would read the old destination, both would mint a secret,
and one of them would walk away holding a secret this service never stored.

## SQLite is not a production target

It has no row locking. It silently ignores `FOR UPDATE`, and it serialises
writers instead. That is enough for the fast test suite, where it only has to
show that the arithmetic and the conditional guards are right.

Everything on this page that matters under load is therefore tested against a
real PostgreSQL server, with real connections, in
`tests/integration/`. See [Testing](../development/testing.md).

## Related

- [Delivery semantics](delivery-semantics.md) for leases and the crash window
- [Rate limiting](../guides/rate-limiting.md) for the operator view
- [Testing](../development/testing.md) for how these are exercised

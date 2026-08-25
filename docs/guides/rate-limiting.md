# Rate Limiting

`POST /f/{endpoint_id}` is public and stays public, which means anyone who can
reach it can send it traffic. Two limits bound how much.

| Limit | Counts | Default |
| --- | --- | --- |
| Per source | Attempts one client address makes, across every endpoint | 60 per 60 seconds |
| Per endpoint | Attempts one endpoint receives, from every source together | 600 per 60 seconds |

A submission must satisfy **both**. The per-source limit stops one client
flooding many endpoints; the per-endpoint limit stops one endpoint consuming the
whole deployment's capacity, including under an attack spread across thousands of
addresses that each stay under the per-source limit.

Neither limit applies to `GET /health` or to any management route. Ingestion
traffic cannot lock an operator out of their own service.

!!! note "This is traffic protection, not spam protection"

    Rate limiting bounds how much a source or an endpoint can send. It has no
    opinion whatsoever about what is in a submission. There is no CAPTCHA and no
    content classification.

## Being refused

```
HTTP/1.1 429 Too Many Requests
Retry-After: 30
```

```json
{
  "error": {
    "code": "rate_limit_exceeded",
    "message": "Too many submission attempts. Try again in 30 seconds.",
    "details": {
      "scope": "ip",
      "limit": 60,
      "window_seconds": 60,
      "retry_after_seconds": 30
    }
  }
}
```

`scope` is `ip` or `endpoint`, and `Retry-After` is whole seconds until the
window that refused you ends.

Which limit tripped is told to you deliberately: a developer whose own client is
looping and one whose form is being flooded from elsewhere need to do completely
different things about it, and anybody could distinguish the two anyway by trying
the same endpoint from a second address. What is never returned is the counter,
the subject it is keyed by, or anything naming a column.

## What counts as an attempt

Every request that reaches the ingestion route spends a unit of budget, **whether
or not it is accepted**. A malformed body, an unsupported content type, an empty
submission and a submission to a disabled endpoint all cost the sender the same as
a successful one, because they all cost this service the same work. Abuse traffic
that is invalid is still abuse traffic.

The order is fixed and worth knowing:

| Step | Effect |
| --- | --- |
| Body size cap, in middleware | An oversized body is refused with `413` and spends nothing |
| Per-source limit | Always spent, before the endpoint ID is even checked |
| Endpoint lookup | An unknown endpoint returns `404` |
| Per-endpoint limit | Spent for any endpoint that exists, active or not |
| Content type, body parse, storage | Only reached once both limits have allowed the attempt |

Two consequences follow from that order, and both are deliberate:

- **An attempt the endpoint limit refuses has already spent the source's
  budget.** Otherwise hammering a saturated endpoint would be free, and an
  attacker could keep a source address permanently under its own limit while
  doing nothing but flooding.
- **An attempt the source limit refuses does not spend an endpoint's budget.**
  The endpoint is never looked up, so a blocked address cannot burn through the
  budget of an endpoint it is not being allowed to reach. A submission to an
  endpoint ID that does not exist spends the source's budget and creates no
  endpoint counter, so guessing identifiers cannot be used to choose how much
  this table grows.

**Idempotent replays count.** A repeated `Idempotency-Key` is still a request
that crosses the network and reaches the database, and exempting it would make
one leaked key an unlimited way past both limits. A replay the limits allow
behaves exactly as it did before: it returns the original submission and queues
no second delivery. See [Idempotency](idempotency.md).

## Which address you are counted as

By default the client address is the **socket peer address** the ASGI server
reports, and `X-Forwarded-For` is ignored entirely. That header is text the
client writes, so trusting it by default would hand every client its own private
rate limit.

!!! warning "Behind a reverse proxy, the socket peer is your proxy"

    Without configuration every visitor would share one bucket. Set
    `FORMS_TRUSTED_PROXY_HOPS` to the number of proxies of your own in front of
    the process. Full setup, including the nginx directive, is in
    [Reverse proxy](../operations/reverse-proxy.md).

```bash
FORMS_TRUSTED_PROXY_HOPS=1
```

Each proxy appends the address it saw, so the entry that many places from the
**right** of `X-Forwarded-For` is the one your outermost proxy observed;
everything to the left of it was written by somebody who is not yours to trust.
Set it to the real number of hops and never higher: a value larger than your
actual chain lets a client insert entries and pick its own bucket. If the header
is missing, or carries fewer entries than you configured, the socket peer is used
instead rather than the header being half believed.

## How addresses are stored

Addresses are stored as a SHA-256 digest, never as text, and no route or log line
returns one.

Setting `FORMS_RATE_LIMIT_IP_SECRET` keys that digest with HMAC and makes it
genuinely one way. Without it the digest is obfuscation only, because the IPv4
space is small enough for anybody holding the table to enumerate.

The usual argument against adding a second secret does not apply here: these
counters live for one window, so changing or losing the secret costs at most one
window of accounting. Every API process must be given the same value, or they
will count the same client under different subjects.

## How the limits are enforced

Counters are rows in PostgreSQL, keyed by limiter, subject and window start, and
incremented with a single `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`. That
one statement is the whole concurrency argument: reading a counter, comparing it
in Python and writing it back would let two simultaneous requests both see room
and both pass. Here the database settles it and hands each request a different
number, so at most one of them can be the last one under the limit.

**The limit is shared, not per process.** Two API replicas enforce one limit
between them rather than one each, which is the entire reason the state is in the
database. There is no Redis, no external rate-limit service and no sticky-session
requirement.

This is tested against real PostgreSQL with several independently built
applications, each with its own engine and connection pool, submitting at the
same instant. See [Concurrency](../architecture/concurrency.md).

## Fixed windows, and their known edge

The algorithm is a **fixed window**. The current window is `now` floored to a
multiple of the window length, against the Unix epoch, so every process derives
the same boundary. It is deterministic and cheap.

!!! warning "The boundary is soft"

    A client that spends a whole window just before it ends and another just
    after can make **twice** the configured requests across those two windows.
    Set the window shorter if that burst matters more to you than the smaller
    counters a longer window keeps.

A sliding window or token bucket would smooth that out at the cost of keeping a
log of request instants or a refill timestamp, which is not worth it for a first
layer whose job is to stop unbounded traffic rather than shape well-behaved
traffic.

## Cleanup

Old windows are removed opportunistically: a small fraction of submission
attempts also delete counters whose window ended several windows ago. The cutoff
is far enough back that a sweep can never take a window still being counted in,
and the delete rides an index on the window column rather than scanning. There is
no extra daemon to deploy for it.

## Settings

| Variable | Default | Meaning |
| --- | --- | --- |
| `FORMS_RATE_LIMIT_ENABLED` | `true` | Enforce the limits at all |
| `FORMS_RATE_LIMIT_IP_REQUESTS` | `60` | Attempts one source may make per window |
| `FORMS_RATE_LIMIT_IP_WINDOW_SECONDS` | `60` | How long the per-source window lasts |
| `FORMS_RATE_LIMIT_ENDPOINT_REQUESTS` | `600` | Attempts one endpoint may receive per window |
| `FORMS_RATE_LIMIT_ENDPOINT_WINDOW_SECONDS` | `60` | How long the per-endpoint window lasts |
| `FORMS_RATE_LIMIT_IP_SECRET` | unset | Secret keying the address digest |
| `FORMS_TRUSTED_PROXY_HOPS` | `0` | Reverse proxies of your own in front of this process |

Set `FORMS_RATE_LIMIT_ENABLED=false` to turn all of this off for local
development. It is on by default, and should stay on in production.

## Related

- [Reverse proxy](../operations/reverse-proxy.md) for the trust model in practice
- [Form ingestion](form-ingestion.md) for what the route accepts
- [Limitations](../reference/limitations.md) for what rate limiting does not solve

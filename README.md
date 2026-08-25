<p align="center">
  <img src="docs/images/logo1.png"
       alt="Hymical Forms logo"
       width="600">
</p>

<p align="center">
  Reliable form ingestion and webhook delivery for developers.
</p>

<p align="center">
  <a href="https://github.com/hymical/forms/actions">
    <img src="https://github.com/hymical/forms/actions/workflows/ci.yml/badge.svg"
         alt="CI">
  </a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue"
       alt="Python 3.11+">
  <a href="https://github.com/hymical/forms/blob/main/LICENSE">
    <img src="https://img.shields.io/badge/license-Apache%202.0-blue"
         alt="Apache License 2.0">
  </a>
  <img src="https://img.shields.io/badge/FastAPI-0.141%2B-009688"
       alt="FastAPI">
  <img src="https://img.shields.io/badge/PostgreSQL-production%20database-4169E1"
       alt="PostgreSQL">
</p>

## The problem

Every project with a contact form, a waitlist, or a feedback box ends up needing
the same small backend: something that accepts an HTML form POST, validates it,
stores it, and forwards it somewhere useful. Writing that once is easy; running
it reliably, with retries, delivery logs, spam handling and retention rules,
is not. Hymical Forms is intended to be that backend, self-hostable and
open-source.

## Project status

**Early development.** This build registers endpoints, stores the submissions
sent to them together with the durable obligation to deliver them, and runs a
separate worker that performs the signed webhook delivery and retries it. An
operator can now list and reconfigure endpoints, read the delivery queue and its
attempt history, and put a failed delivery back in the queue, all through the
same authenticated management API.

Everything that administers the service requires a management API key. Form
ingestion stays public, because an ingestion URL is meant to sit in the `action`
attribute of somebody's HTML form. Public submissions are now rate limited per
source address and per endpoint, which bounds the volume one deployment will
accept. That is traffic protection and nothing more: **there is still no spam
protection**, no CAPTCHA and no content classification, so a public deployment
will accept junk up to the configured rate.

| Capability                     | Status                    |
| ------------------------------ | ------------------------- |
| Health endpoint                | Implemented               |
| Form ingestion + validation    | Implemented               |
| Request limits + error model   | Implemented               |
| Endpoint registry              | Implemented               |
| Submission persistence         | Implemented               |
| Idempotent retries             | Implemented               |
| Signed webhook delivery        | Implemented               |
| Durable delivery queue         | Implemented               |
| Retries with backoff           | Implemented               |
| Schema migrations              | Implemented               |
| API keys / authentication      | Implemented               |
| Endpoint management            | Implemented               |
| Delivery inspection            | Implemented               |
| Manual delivery replay         | Implemented               |
| Public ingestion rate limiting | Implemented               |
| Endpoint deletion              | **Not implemented**       |
| Submission retrieval           | **Not implemented**       |
| Spam handling, CAPTCHA         | **Not implemented**       |
| Export, retention, dashboards  | **Not implemented**       |

## Requirements

- Python 3.11 or newer
- PostgreSQL, which is the intended production database

SQLite is supported for local experimentation and backs the test suite. It is
not a supported production target.

## Install

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

On Windows, activate with `.venv\Scripts\activate` instead.

## Configure

`FORMS_DATABASE_URL` is required and has no default. Set it in the environment
or in a `.env` file in the working directory:

```bash
FORMS_DATABASE_URL=postgresql+psycopg://forms:forms@localhost:5432/forms
```

To try the service without running PostgreSQL:

```bash
FORMS_DATABASE_URL=sqlite:///./forms.db
```

See [`.env.example`](.env.example) for every setting and its default.

## Run

Hymical Forms is two processes sharing one database. Migrate it first, create a
management API key, then start them:

```bash
alembic upgrade head
```

```bash
python -m hymical_forms.cli create-key --name local-admin
```

```bash
uvicorn hymical_forms.main:app --reload
```

```bash
python -m hymical_forms.worker
```

The **API** accepts submissions, stores them, and records that a webhook is owed.
It never makes an outbound request. The **worker** claims owed deliveries, sends
them, and retries the ones that fail. Running the API alone is fine: submissions
are still accepted and nothing is lost, they simply wait until a worker exists.

Neither process creates or alters the schema. Both check on startup that the
database is reachable and at the migration revision the build was written
against, and refuse to start otherwise:

```
the database is at migration '0003' but this build expects '0004'.
Run 'alembic upgrade head' before starting.
```

Migrating is an operator action, run when the operator chooses. See
[Schema migrations](#schema-migrations).

## Schema migrations

Alembic owns the schema. Neither the API nor the worker creates or alters a
table: they check on startup that the database is at the revision they were
built against, and stop if it is not.

### A fresh database

```bash
createdb forms
export FORMS_DATABASE_URL=postgresql+psycopg://forms:forms@localhost:5432/forms
alembic upgrade head
```

That is the whole setup. Migrations read `FORMS_DATABASE_URL`, the same setting
the application reads, so there is nothing extra to configure and no credentials
in any tracked file. To migrate a different database without changing your
environment:

```bash
alembic -x database_url=postgresql+psycopg://user:pass@host/other upgrade head
```

### Upgrading an existing database

```bash
alembic upgrade head        # apply everything outstanding
```

Run it before starting the new build. The usual order for a deploy is: stop the
old processes, migrate, start the new ones. Migrating while an old build is
still running is only safe if the change happens to be backwards compatible,
and this project does not promise that for any particular migration.

Useful alongside it:

```bash
alembic current             # what revision is this database at
alembic history --verbose   # what revisions exist
alembic upgrade head --sql  # print the SQL instead of applying it, for review
alembic downgrade -1        # step back one revision
```

`--sql` is worth knowing about: it lets whoever owns the production database
read the DDL before anything touches it.

### SQLite

Migrations run against SQLite too, so local experimentation works the same way:

```bash
export FORMS_DATABASE_URL=sqlite:///./forms.db
alembic upgrade head
```

Migrations that alter a column are written in batch mode, because SQLite cannot
`ALTER` in place and has to rebuild the table instead. This is configured
already; it is not something a migration author has to remember.

### Writing a migration

```bash
alembic revision --autogenerate -m "what changed"
```

**Read what it produces before committing it.** Autogenerate is a starting
point, not an answer: it does not always render custom column types in a usable
way, and it cannot see anything the models do not declare. The PostgreSQL suite
asserts that migrations and models describe the same schema, so drift fails the
build rather than surfacing in production.

Interactive API documentation is served at `http://127.0.0.1:8000/docs`.

## Management API keys

Administering the service needs a credential. Submitting a form does not.

| Route                                   | Credential                  |
| --------------------------------------- | --------------------------- |
| `POST /endpoints`                        | Management API key required |
| `GET /endpoints`                         | Management API key required |
| `GET /endpoints/{endpoint_id}`           | Management API key required |
| `PATCH /endpoints/{endpoint_id}`         | Management API key required |
| `GET /deliveries`                        | Management API key required |
| `GET /deliveries/{delivery_id}`          | Management API key required |
| `POST /deliveries/{delivery_id}/replay`  | Management API key required |
| `POST /f/{endpoint_id}`                  | **Public**, no credential   |
| `GET /health`                            | **Public**, no credential   |

A management API key is an opaque bearer token that administers the whole
service. It is not a user account, not a login, and not scoped to a tenant:
there are no users, accounts, organizations, roles or permissions in this build,
and every valid key can do everything a management key can do. It is also
completely separate from a webhook signing secret. A `whsec_...` secret proves
to *your* server that a delivery came from Hymical Forms; a `hym_live_...` key
proves to Hymical Forms that a management request came from you. Neither can be
used in place of the other.

### Creating the first key

Keys are created against the database with the operator CLI, never over HTTP.
A route that issued a management credential without needing one would be an
unauthenticated way to gain full management access, which is the thing this
boundary exists to remove. Nothing is generated at startup, and no key ships
with this repository.

```bash
python -m hymical_forms.cli create-key --name local-admin
```

```
Created management API key mk_634c4efc22fb40fab4b19b82202b23bb (local-admin).

    hym_live_EXAMPLEONLYNOTAREALKEYREPLACETHISWITHYOURS

Save this key now. It is shown here and nowhere else: the server stores
only a digest of it and cannot show it again. If you lose it, create a
replacement and revoke this one by its key ID.
```

> **Save the key now.** That line is the only time it is ever displayed. The
> server stores a SHA-256 digest of it and nothing else, so there is no command,
> route or database query that can show it to you again. Losing a key means
> creating another one and revoking the old one by its key ID, which `list-keys`
> can still tell you.

The command reads `FORMS_DATABASE_URL`, the same setting everything else reads,
and refuses to run against a database that is not at the migration revision this
build expects.

**A key is not configuration.** There is no `FORMS_API_KEY` variable, and there
should not be: keys live in the database so they can be created and revoked
without restarting anything, and so revoking one takes effect on the very next
request rather than on the next deploy.

### Authenticating a management request

```bash
curl -X POST http://127.0.0.1:8000/endpoints \
  -H 'Authorization: Bearer hym_live_REPLACE_WITH_YOUR_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"id": "contact-form", "name": "Contact form"}'
```

The `Bearer` scheme is required. Credentials are never accepted in a query
parameter, where they would end up in access logs, browser history and
`Referer` headers.

### Listing keys

```bash
python -m hymical_forms.cli list-keys
```

```
KEY ID                               NAME         PREFIX             CREATED                    LAST USED  STATUS
mk_634c4efc22fb40fab4b19b82202b23bb  local-admin  hym_live_EXAMPLE1  2026-08-24T22:53:28+00:00  never      active
```

Only what the database holds, which is no credential. `PREFIX` is the first few
characters of the key, kept so that you can tell two credentials apart in this
listing and match one against the key you saved. There is no HTTP route that
lists keys.

### Revoking a key

```bash
python -m hymical_forms.cli revoke-key mk_634c4efc22fb40fab4b19b82202b23bb
```

The key stops authenticating on the next request. Nothing caches a credential,
so revocation is immediate rather than eventual. The row is not deleted: it
keeps the key's identity, when it was created, when it was last used and when it
was withdrawn, which is what makes a revoked key still explainable later.
Revoking twice is accepted and reports the moment the key actually stopped
working. Rotation beyond create plus revoke is not implemented: to rotate,
create a new key, move your callers onto it, then revoke the old one.

### Key format

A key is `hym_live_` followed by 32 random bytes from the operating system,
rendered as unpadded base64url: 43 characters, 256 bits of entropy. The prefix
is there so an operator who finds the string in a config file knows immediately
whose credential it is and what to revoke.

Only a hex SHA-256 digest of the whole key is stored, and lookup is by that
digest, so authentication is one indexed read. Password-style slow hashing is
deliberately not used: it exists because passwords are low-entropy and guessable,
and it would only add latency to every management request here. There is no
server-side pepper either, for the same reason: a pepper protects a digest that
is feasible to attack offline, which a 256-bit random secret is not, and it would
add a second secret whose loss would silently invalidate every key in the table.

## API

### `GET /health`

Reports that the API process is running.

```json
{ "status": "ok", "service": "hymical-forms", "version": "0.1.0" }
```

This is a liveness signal only. It does not check the database, so it stays
answerable while the database is down, which is what makes it useful for
deciding whether to restart the process.

### `POST /endpoints`

Registers a form endpoint. Submissions are only accepted for endpoints that
exist here.

**This route requires a management API key.** See
[Management API keys](#management-api-keys) for how to create one. A request
without a usable one is refused with `401 authentication_required`, and one
carrying a credential that does not authenticate with `401 invalid_api_key`.

```bash
curl -X POST http://127.0.0.1:8000/endpoints \
  -H 'Authorization: Bearer hym_live_REPLACE_WITH_YOUR_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"id": "contact-form", "name": "Contact form",
       "webhook_url": "https://example.com/hooks/forms"}'
```

| Field         | Required | Meaning                                            |
| ------------- | -------- | -------------------------------------------------- |
| `id`          | yes      | The public identifier the endpoint answers on      |
| `name`        | yes      | Human-readable label, 1 to 200 characters          |
| `is_active`   | no       | Whether it accepts submissions, defaults to `true` |
| `webhook_url` | no       | Where accepted submissions are delivered           |

**Endpoint IDs** are supplied by you, not generated, because the ID appears in
the `action` URL of your HTML form and a memorable one is worth more than an
opaque one. An ID is 3 to 64 characters of lowercase ASCII letters, digits, `-`
and `_`, and must start and end with a letter or digit. It is also the primary
key, so it cannot be changed later.

Returns `201 Created`:

```json
{
  "id": "contact-form",
  "name": "Contact form",
  "is_active": true,
  "created_at": "2026-08-24T14:34:27.432598Z",
  "webhook_url": "https://example.com/hooks/forms",
  "webhook_secret": "whsec_6f1c...  (64 hex characters)"
}
```

> **Save `webhook_secret` now.** It is generated by the server, returned only in
> this response, and there is no route that reads it back. Losing it means
> creating a new endpoint.

Reusing an ID returns `409 endpoint_already_exists`. To change an endpoint
afterwards, see [Managing endpoints](#managing-endpoints).

The key that created an endpoint is not recorded on it. A management key
administers the service rather than owning a slice of it, and there is no
tenancy model for an owner column to belong to.

### Managing endpoints

Every route below takes the same credential as `POST /endpoints`. The examples
use an obvious placeholder for it:

```bash
export HYMICAL_KEY=hym_live_REPLACE_WITH_YOUR_KEY
```

#### `GET /endpoints`

```bash
curl "http://127.0.0.1:8000/endpoints?limit=2" \
  -H "Authorization: Bearer $HYMICAL_KEY"
```

```json
{
  "items": [
    {
      "id": "contact-form",
      "name": "Contact form",
      "is_active": true,
      "created_at": "2026-08-24T14:34:27.432598Z",
      "webhook_url": "https://example.com/hooks/forms"
    }
  ],
  "next_cursor": null
}
```

`webhook_url` is returned because it is configuration rather than a credential:
knowing where an endpoint delivers proves nothing and forges nothing. The
signing secret is not returned, here or anywhere else. See
[Pagination](#pagination) for what `next_cursor` does.

#### `GET /endpoints/{endpoint_id}`

```bash
curl http://127.0.0.1:8000/endpoints/contact-form \
  -H "Authorization: Bearer $HYMICAL_KEY"
```

The same representation as one item of the listing, for one endpoint, so a
caller that already knows an ID does not have to page to find it. An unknown ID
returns `404 endpoint_not_found`.

#### `PATCH /endpoints/{endpoint_id}`

`PATCH` rather than `PUT`, because only a few fields are yours to change. Send
only what should change; anything omitted is left exactly as it was.

```bash
curl -X PATCH http://127.0.0.1:8000/endpoints/contact-form \
  -H "Authorization: Bearer $HYMICAL_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"name": "Support form", "is_active": false}'
```

| Field         | Meaning                                                          |
| ------------- | ---------------------------------------------------------------- |
| `name`        | New label, 1 to 200 characters                                    |
| `is_active`   | Whether the endpoint accepts submissions                          |
| `webhook_url` | New destination, or `null` to remove the webhook entirely         |

**The endpoint ID is not changeable.** It is the primary key, and it appears in
the `action` URL of every HTML form pointing at the endpoint, so changing it
would break deployed forms. An `id` in the body is ignored.

**Deleting an endpoint is not implemented.** Disabling one is enough for now:
deletion immediately raises what happens to its stored submissions, its delivery
history and the foreign keys between them, and none of that is decided yet.

#### Disabling and re-enabling

```bash
curl -X PATCH http://127.0.0.1:8000/endpoints/contact-form \
  -H "Authorization: Bearer $HYMICAL_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"is_active": false}'
```

Disabling takes effect on the very next submission, which is refused with
`409 endpoint_inactive` and stores nothing. Nothing caches the endpoint, so
there is no window in which a disabled endpoint still accepts a form. Sending
`{"is_active": true}` restores acceptance just as immediately.

Deliveries that were already queued are **not** affected. They are work this
service already promised to do, and disabling an endpoint stops it taking on
more rather than abandoning what it owes.

#### Changing the webhook

The destination and its signing secret change together, because a secret belongs
to a receiver rather than to an endpoint:

| Change                              | What happens to the secret            |
| ----------------------------------- | ------------------------------------- |
| `webhook_url` omitted                | Unchanged                             |
| `webhook_url` set to the same URL    | Unchanged                             |
| `webhook_url` set to a different URL | A new one is generated and returned   |
| `webhook_url` set on an endpoint with no webhook | A new one is generated and returned |
| `webhook_url` set to `null`          | Removed along with the destination    |

```json
{
  "id": "contact-form",
  "name": "Contact form",
  "is_active": true,
  "created_at": "2026-08-24T14:34:27.432598Z",
  "webhook_url": "https://example.com/hooks/forms-v2",
  "webhook_secret": "whsec_6f1c...  (64 hex characters)"
}
```

> **Save `webhook_secret` now.** As at creation, it is returned only in the
> response of the request that generated it. `webhook_secret` is `null` when the
> request did not generate one, and the field does not exist at all on a read.

Carrying the old secret over to a new destination would hand a receiver that
never had it the ability to verify signatures, and leave the previous receiver
holding a live secret. So the two always move together.

**Deliveries already queued keep the destination and secret they snapshotted**
when their submission was accepted. Changing configuration here never redirects
work that is already owed, and never leaves a queued payload signed with a
secret its receiver never had.

### `POST /f/{endpoint_id}`

Accepts a form submission for a registered endpoint and stores it.

**This route is public and stays public.** It is the URL that goes in the
`action` attribute of an HTML form, so it cannot require a header a browser form
has no way to send. No management credential is read here, and one sent anyway
is ignored rather than forwarded anywhere.

**It is rate limited**, per source address and per endpoint, and an attempt over
either limit is refused with `429 rate_limit_exceeded` and a `Retry-After`
header. See [Rate limits](#rate-limits) for the defaults, what counts as an
attempt, and how the client address is determined.

A submission to an ID that does not exist is rejected with
`404 endpoint_not_found`, and one to an inactive endpoint with
`409 endpoint_inactive`. Neither leaves anything in the database.

**Content types.** `application/x-www-form-urlencoded` and
`multipart/form-data` are both accepted, so a plain HTML `<form>` works
unchanged. File uploads are not: a multipart part carrying a file is rejected
rather than silently dropped. Anything else is rejected with `415`.

**Repeated field names**, such as checkbox groups and multi-selects, are
preserved in order, both in the response count and in storage. No submitted
value is discarded.

A successful request returns `202 Accepted`. The status is deliberately not
`201`: the submission is stored and any delivery it owes is queued, but that
delivery has not happened yet.

```json
{
  "submission_id": "sub_48984534f33749c49a88de2d59400dce",
  "endpoint_id": "contact-form",
  "received_at": "2026-08-24T14:34:27.651841Z",
  "field_count": 3,
  "idempotent_replay": false,
  "delivery": { "queued": true }
}
```

Submitted values are not echoed back, because the client already has them.

`delivery.queued` says whether a webhook delivery is owed for this submission.
No outbound request is made during this request, so the response says nothing
about whether a destination is reachable: that is the worker's business, and a
destination being down can no longer affect whether a form is accepted.

### Rate limits

`POST /f/{endpoint_id}` is public and stays public, which means anyone who can
reach it can send it traffic. Two limits bound how much.

| Limit           | Counts                                              | Default            |
| --------------- | --------------------------------------------------- | ------------------ |
| Per source      | Attempts one client address makes, across every endpoint | 60 per 60 seconds  |
| Per endpoint    | Attempts one endpoint receives, from every source together | 600 per 60 seconds |

A submission must satisfy **both**. The per-source limit stops one client
flooding many endpoints; the per-endpoint limit stops one endpoint consuming the
whole deployment's capacity, including under an attack spread across thousands of
addresses that each stay under the per-source limit.

Neither limit applies to `GET /health` or to any management route. Ingestion
traffic cannot lock an operator out of their own service.

#### Being refused

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
window that refused you ends. Which limit tripped is told to you deliberately: a
developer whose own client is looping and one whose form is being flooded from
elsewhere need to do completely different things about it, and anybody could
distinguish the two anyway by trying the same endpoint from a second address.
What is never returned is the counter, the subject it is keyed by, or anything
naming a column.

#### What counts as an attempt

Every request that reaches the ingestion route spends a unit of budget, **whether
or not it is accepted**. A malformed body, an unsupported content type, an empty
submission and a submission to a disabled endpoint all cost the sender the same
as a successful one, because they all cost this service the same work. Abuse
traffic that is invalid is still abuse traffic.

The order is fixed and worth knowing:

| Step                                | Effect                                                     |
| ----------------------------------- | ---------------------------------------------------------- |
| Body size cap, in middleware         | An oversized body is refused with `413` and spends nothing  |
| Per-source limit                     | Always spent, before the endpoint ID is even checked        |
| Endpoint lookup                      | An unknown endpoint returns `404`                           |
| Per-endpoint limit                   | Spent for any endpoint that exists, active or not           |
| Content type, body parse, storage    | Only reached once both limits have allowed the attempt      |

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
no second delivery.

#### Which address you are counted as

By default the client address is the **socket peer address** the ASGI server
reports, and `X-Forwarded-For` is ignored entirely. That header is text the
client writes, so trusting it by default would hand every client its own private
rate limit.

**If you run this behind a reverse proxy, the socket peer is your proxy**, and
without configuration every visitor would share one bucket. Set
`FORMS_TRUSTED_PROXY_HOPS` to the number of proxies of your own in front of the
process:

```bash
FORMS_TRUSTED_PROXY_HOPS=1
```

Each proxy appends the address it saw, so the entry that many places from the
**right** of `X-Forwarded-For` is the one your outermost proxy observed;
everything to the left of it was written by somebody who is not yours to trust.
Set it to the real number of hops and never higher: a value larger than your
actual chain lets a client insert entries and pick its own bucket. If the header
is missing, or carries fewer entries than you configured, the socket peer is used
instead rather than the header being half believed. Make sure your proxy is
actually appending the header (nginx: `proxy_set_header X-Forwarded-For
$proxy_add_x_forwarded_for`).

Addresses are stored as a SHA-256 digest, never as text, and no route or log line
returns one. Setting `FORMS_RATE_LIMIT_IP_SECRET` keys that digest with HMAC and
makes it genuinely one way; without it the digest is obfuscation only, because the
IPv4 space is small enough for anybody holding the table to enumerate. The usual
argument against adding a second secret does not apply here: these counters live
for one window, so changing or losing the secret costs at most one window of
accounting. Every API process must be given the same value.

#### How the limits are enforced

Counters are rows in PostgreSQL, keyed by limiter, subject and window start, and
incremented with a single `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`. That
one statement is the whole concurrency argument: reading a counter, comparing it
in Python and writing it back would let two simultaneous requests both see room
and both pass. Here the database settles it and hands each request a different
number, so at most one of them can be the last one under the limit. This is
tested against real PostgreSQL with several independently built applications,
each with its own engine and connection pool, submitting at the same instant.

**The limit is shared, not per process.** Two API replicas enforce one limit
between them rather than one each, which is the entire reason the state is in the
database. There is no Redis, no external rate-limit service and no sticky-session
requirement.

The algorithm is a **fixed window**: the current window is `now` floored to a
multiple of the window length, against the Unix epoch, so every process derives
the same boundary. It is deterministic and cheap, and its known weakness is the
boundary. A client that spends a whole window just before it ends and another
just after can make twice the configured requests across those two windows. A
sliding window or token bucket would smooth that out at the cost of keeping a log
of request instants or a refill timestamp, which is not worth it for a first
layer whose job is to stop unbounded traffic rather than shape well-behaved
traffic.

Old windows are removed opportunistically: a small fraction of submission
attempts also delete counters whose window ended several windows ago. The cutoff
is far enough back that a sweep can never take a window still being counted in,
and the delete rides an index on the window column rather than scanning. There is
no extra daemon to deploy for it.

Set `FORMS_RATE_LIMIT_ENABLED=false` to turn all of this off for local
development. It is on by default, and should stay on in production.

### Retrying safely with `Idempotency-Key`

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

**Retry semantics.** Repeating the same key on the same endpoint with the same
content returns `202` with the *original* `submission_id` and `received_at`, and
`"idempotent_replay": true`. Only one row is ever stored, and this holds even
when the retries arrive at the same instant: the database, not the application,
is what decides the winner.

The server does not retry anything on your behalf. It only makes your retries
safe.

**Conflict semantics.** Reusing a key on the same endpoint with *different*
content is rejected with `409 idempotency_conflict`. The stored submission is
left exactly as it was, and the response never describes its contents.

Content is compared by a SHA-256 fingerprint of the normalized fields. Because
field order and repeated values are meaningful to this service, they are
meaningful to the fingerprint too: `a=1&b=2` and `b=2&a=1` are different
payloads and will conflict. The generated submission ID and the received
timestamp are excluded, so an honest retry always matches.

**Scope.** A key belongs to one endpoint. The same key may be used once per
endpoint without conflicting, and there is no expiry: a key is spent for as long
as its submission is stored.

**Key format.** 16 to 255 printable ASCII characters with no spaces, which
accepts UUIDs, hex, base64 and base64url. Anything else is rejected with
`400 invalid_idempotency_key`, including a header that is present but empty.

The 16-character floor exists because keys are endpoint-scoped and form
ingestion is public, so every client of an endpoint shares one key space. A short
or predictable key would collide with a stranger's submission. Use a random
value. Management API keys do not change this: they authenticate the endpoint's
administrator, not the visitors submitting the form.

### Webhook delivery

If an endpoint has a `webhook_url`, accepting a submission also writes a durable
delivery record in the **same transaction**. Nothing is sent during the form
request. A worker picks the delivery up, sends it, and retries it if it fails.

That transaction is the whole reliability claim. Once `POST /f/{endpoint_id}`
answers `202`, the submission is stored *and* the obligation to deliver it is
stored. A crash at any point after that cannot lose the delivery, because it is
a row rather than a thing the API process was about to do.

The submission response reports only whether work was queued:

```json
"delivery": { "queued": true }
```

`queued` is false when the endpoint has no webhook. An idempotent replay reports
`true` and does **not** queue a second delivery: one submission owes at most one
delivery, enforced by a unique constraint on the submission.

#### Payload

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

#### Verifying the signature

Signing is unchanged from the previous release. Each request carries a
`Hymical-Signature` header:

```
Hymical-Signature: v1=9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08
```

The digest is HMAC-SHA256 of the **raw request body**, keyed with the endpoint's
`webhook_secret`. Verify against the exact bytes you received, before parsing
the JSON: re-serializing the payload will produce different bytes and fail.

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

Use a constant-time comparison, as `hmac.compare_digest` does above. The `v1=`
prefix exists so a future scheme can be added without breaking receivers that
only understand this one.

A delivery keeps the destination and secret that were configured when the
submission was accepted. Changing an endpoint's webhook does not redirect
deliveries that are already queued, and does not leave a queued payload signed
with a secret its receiver never had.

#### Delivery states

| State        | Meaning                                                 |
| ------------ | ------------------------------------------------------- |
| `pending`    | Owed, waiting for its next attempt time                  |
| `processing` | Claimed by a worker, holding a lease                     |
| `delivered`  | A destination answered `2xx`. Terminal                   |
| `failed`     | Given up on. Terminal                                    |

#### What retries, and what does not

| Outcome                            | Retried |
| ---------------------------------- | ------- |
| Connection failure                  | yes     |
| Timeout                             | yes     |
| HTTP `5xx`                          | yes     |
| HTTP `408`, `425`, `429`            | yes     |
| HTTP `2xx`                          | delivered |
| Any other `4xx`, including `409`    | no      |
| `3xx`                               | no      |

Ordinary `4xx` responses are final because repeating a request the receiver
called malformed, unauthorized or missing will not repair it. `409` is treated
as final too: from a webhook receiver it almost always means "I already have
this event". Redirects are still not followed, and a `3xx` is a misconfiguration
rather than a passing problem, so it is final as well.

#### Retry schedule

Delivery is attempted immediately, then backs off by doubling, capped:

| Attempt | Waits before it |
| ------- | --------------- |
| 1       | none            |
| 2       | 10s             |
| 3       | 20s             |
| 4       | 40s             |
| 5       | 80s             |

After `FORMS_WEBHOOK_MAX_ATTEMPTS` (default 5) the delivery becomes `failed` and
is never retried automatically. There is no jitter: the schedule is deliberately
exactly predictable. Every attempt, including the last, stays in
`delivery_attempts`.

The allowance is per retry cycle, not per lifetime, so a
[manual replay](#manual-delivery-replay) starts the schedule again from the top.

#### At-least-once, not exactly-once

A worker claims a delivery by taking a lease on it. If the worker dies, the
lease expires and another worker picks the delivery up, which is what stops a
crash from stranding work in `processing` forever.

**This means duplicate delivery is possible.** A worker that sends successfully
and then dies before recording that success leaves a delivery that looks unsent,
and the next worker sends it again. No queue can close this window on its own;
it needs the receiver's cooperation. Use the `id` in the signed payload to
ignore an event you have already processed. Hymical Forms does not offer
exactly-once delivery and there is no message broker involved: PostgreSQL is
the queue.

#### What is recorded

`webhook_deliveries` holds one row per logical delivery: its state, how many
attempts it has had, when it is next due, and when it finished.
`delivery_attempts` holds one row per request that actually went out, numbered,
with the outcome, the HTTP status when there was one, and a bounded failure
message. Response bodies are **not** stored, and neither is the signing secret.
A job that is inspected and found not due records nothing. Both are readable
through [Inspecting deliveries](#inspecting-deliveries).

#### Destinations that are refused

A webhook URL must use `http` or `https` and must not name a loopback, private,
link-local, multicast, reserved or unspecified address. That covers `localhost`,
`127.0.0.1`, `[::1]`, `[::ffff:127.0.0.1]`, `10.0.0.0/8`, `192.168.0.0/16`, and
the `169.254.169.254` cloud metadata endpoint. Rejections return
`422 invalid_webhook_url`. The same check runs on a destination supplied through
`PATCH /endpoints/{endpoint_id}`.

This is **not** complete SSRF protection; see [Limitations](#limitations).

For local development, `FORMS_ALLOW_PRIVATE_WEBHOOK_TARGETS=true` lifts the
address restriction so you can point a webhook at a server on your own machine.
Do not enable it in production.

### Inspecting deliveries

The delivery queue is operational data, so reading it needs a management API key.
The examples below use the same placeholder as the endpoint routes:

```bash
export HYMICAL_KEY=hym_live_REPLACE_WITH_YOUR_KEY
```

#### `GET /deliveries`

```bash
curl "http://127.0.0.1:8000/deliveries?state=failed&endpoint_id=contact-form" \
  -H "Authorization: Bearer $HYMICAL_KEY"
```

| Filter        | Meaning                                                          |
| ------------- | ---------------------------------------------------------------- |
| `endpoint_id` | Only deliveries for one endpoint                                  |
| `state`       | One of `pending`, `processing`, `delivered`, `failed`             |
| `limit`       | Page size, 1 to 100, default 50                                   |
| `cursor`      | The previous page's `next_cursor`                                 |

```json
{
  "items": [
    {
      "id": "whd_9a3f...",
      "submission_id": "sub_48984534f33749c49a88de2d59400dce",
      "endpoint_id": "contact-form",
      "state": "failed",
      "destination_url": "https://example.com/hooks/forms",
      "attempt_count": 5,
      "cycle_attempt_count": 5,
      "next_attempt_at": "2026-08-24T15:12:07.117440Z",
      "created_at": "2026-08-24T14:34:27.651841Z",
      "completed_at": "2026-08-24T15:12:07.204118Z"
    }
  ],
  "next_cursor": null
}
```

An unknown `state` is refused with `422 invalid_request`. An `endpoint_id` that
matches nothing is not an error: it is a filter that selected no rows, and the
answer is an empty page.

**Submitted field values are not returned**, in the listing or the detail. There
is no route that reads a submission back yet, and a delivery view is not a way
around that.

#### `GET /deliveries/{delivery_id}`

The same fields as one listed item, plus the ordered attempt history:

```json
{
  "id": "whd_9a3f...",
  "state": "failed",
  "attempt_count": 5,
  "cycle_attempt_count": 5,
  "attempts": [
    {
      "attempt_number": 1,
      "attempted_at": "2026-08-24T14:34:27.884210Z",
      "outcome": "http_error",
      "response_status": 503,
      "error": "destination responded with HTTP 503"
    }
  ]
}
```

Attempts are ordered by `attempt_number`, ascending. `outcome` is one of
`succeeded`, `http_error`, `timeout` or `network_error`. `response_status` is
null when the destination never answered at all. The snapshotted signing secret,
the request headers and the response body are not there: the first two are never
returned by any route, and the third is never stored.

An unknown ID returns `404 delivery_not_found`.

### Manual delivery replay

#### `POST /deliveries/{delivery_id}/replay`

```bash
curl -X POST http://127.0.0.1:8000/deliveries/whd_9a3f.../replay \
  -H "Authorization: Bearer $HYMICAL_KEY"
```

Returns `200 OK` with the delivery's new state:

```json
{
  "id": "whd_9a3f...",
  "state": "pending",
  "attempt_count": 5,
  "cycle_attempt_count": 0,
  "next_attempt_at": "2026-08-24T16:02:11.006318Z",
  "completed_at": null
}
```

**Only a delivery in terminal `failed` can be replayed.** A `pending` one is
already queued, a `processing` one is already being sent, and a `delivered` one
already reached its receiver. All three are refused with
`409 delivery_not_replayable`, and the error names the state that made it
ineligible.

**`200`, not `202`.** A `202` would say this request will be carried out later,
which invites reading the replay as the delivery. It is not. This request
finishes here, and what it leaves behind is a queued row.

#### What replay actually does

Replay is a state change. **The API process sends nothing**: it has no outbound
HTTP client at all, which is what makes that structural rather than a promise.
The delivery becomes due, the ordinary worker claims it on its next poll through
its ordinary claiming path, and the ordinary retry rules apply from there.

```
failed -> replay -> pending and due -> worker claims -> delivered, or retries
```

Everything that identifies the delivery is preserved:

- the **same logical delivery**, not a second one;
- the **same submission**, unmodified and not duplicated;
- the **snapshotted destination and signing secret**, so the receiver verifies
  the replayed payload exactly as it would have verified the original;
- **every historical attempt row**, untouched.

#### Attempt numbering and the retry budget

A delivery carries two counters, because they answer two different questions:

| Field                 | Meaning                                                |
| --------------------- | ------------------------------------------------------ |
| `attempt_count`       | Every request ever made for this delivery               |
| `cycle_attempt_count` | Requests made since it last entered the queue           |

`attempt_count` only ever goes up, and it is what numbers the attempt history,
so **an attempt number is never reused** however often a delivery is replayed.
`cycle_attempt_count` is what the retry allowance is measured against, and a
replay resets it to zero.

That is the whole model: a replay starts a fresh retry cycle while preserving
the history. A delivery that exhausted five automatic attempts and is then
replayed gets five more, numbered 6 through 10, rather than failing immediately
because five have already been spent. Until something is replayed the two
counters are equal.

#### Two operators replaying at once

The transition is a single conditional `UPDATE` on the delivery's state, so the
database decides who wins, not a check in application code. Exactly one request
requeues the delivery. The other reads back the state that was actually settled
on and is refused with `409 delivery_not_replayable`, the same answer it would
get for a delivery that was never failed. No duplicate work is created either
way. This is tested against real PostgreSQL with independent connections.

### Pagination

Both list routes page the same way. Items come back newest first, ordered by
creation time with the identifier as a tie-break, so a page is exactly
reproducible. `limit` is 1 to 100 and defaults to 50; anything outside that is
refused with `422 invalid_request` rather than quietly clamped.

`next_cursor` is the identifier of the last item on the page. Pass it as
`cursor` to continue:

```bash
curl "http://127.0.0.1:8000/deliveries?limit=50&cursor=whd_9a3f..." \
  -H "Authorization: Bearer $HYMICAL_KEY"
```

A full page always carries a cursor, even when it happens to be the last one,
because knowing otherwise would cost an extra read on every page. A walk
therefore ends on one empty page rather than on a null cursor. A cursor that
names no row is refused with `422 invalid_cursor`.

There is deliberately **no total count**. Counting an operational table on every
page is not free, and nothing here needs the number.

### Try it

```bash
python -m hymical_forms.cli create-key --name local-admin
```

```bash
curl -X POST http://127.0.0.1:8000/endpoints \
  -H 'Authorization: Bearer hym_live_REPLACE_WITH_YOUR_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"id": "contact-form", "name": "Contact form"}'
```

Submitting needs no credential at all:

```bash
curl -i -X POST http://127.0.0.1:8000/f/contact-form -d email=dev@example.com -d message=hello
```

Or from a browser, against a locally running server:

```html
<form action="http://127.0.0.1:8000/f/contact-form" method="POST">
  <input type="email" name="email" required />
  <textarea name="message"></textarea>
  <button type="submit">Send</button>
</form>
```

### Errors

Every non-2xx response uses one envelope. `code` is stable and
machine-readable; `details` appears only when there is something concrete to
add.

```json
{
  "error": {
    "code": "too_many_fields",
    "message": "Submission carries 120 fields, which exceeds the limit of 100.",
    "details": { "limit": 100, "received": 120 }
  }
}
```

| Status | `code`                     | Cause                                                    |
| ------ | -------------------------- | -------------------------------------------------------- |
| 400    | `malformed_form_body`      | Body does not parse as the declared content type          |
| 400    | `invalid_idempotency_key`  | `Idempotency-Key` header breaks the key format rules      |
| 401    | `authentication_required`  | A management route was called without a bearer credential |
| 401    | `invalid_api_key`          | The management API key is malformed, unknown or revoked   |
| 404    | `invalid_endpoint_id`      | Submission path is not a well-formed endpoint ID          |
| 404    | `endpoint_not_found`       | Endpoint ID is well formed but no such endpoint exists    |
| 404    | `delivery_not_found`       | No delivery with that ID exists                           |
| 404    | `not_found`                | Unknown path                                              |
| 405    | `method_not_allowed`       | Wrong method for a known path                             |
| 409    | `endpoint_inactive`        | Endpoint exists but is not accepting submissions          |
| 409    | `endpoint_already_exists`  | Endpoint ID is already taken                              |
| 409    | `idempotency_conflict`     | Idempotency key already used for different content        |
| 409    | `delivery_not_replayable`  | Delivery has not terminally failed                        |
| 413    | `request_body_too_large`   | Body exceeded `FORMS_MAX_BODY_BYTES`                      |
| 415    | `unsupported_media_type`   | Content type is not a supported form encoding             |
| 429    | `rate_limit_exceeded`      | A public ingestion rate limit was exhausted               |
| 422    | `empty_submission`         | No fields were submitted                                  |
| 422    | `invalid_cursor`           | Pagination cursor does not continue from a known row      |
| 422    | `invalid_endpoint_id`      | Endpoint ID in a request body breaks the ID rules         |
| 422    | `invalid_request`          | Request body or query parameters failed schema validation |
| 422    | `invalid_webhook_url`      | Webhook destination is malformed or not permitted         |
| 422    | `file_upload_not_supported`| A multipart part carried a file                           |
| 422    | ingestion rule codes       | See below                                                 |
| 500    | `internal_error`           | Unexpected failure; no internals are exposed              |
| 503    | `storage_unavailable`      | The database could not be reached or written to           |

Ingestion rule codes are `too_many_fields`, `field_name_too_long`,
`field_value_too_long`, `invalid_field_name` and `invalid_field_value`.

`invalid_endpoint_id` carries a different status depending on where the ID came
from: `404` when it arrived as a submission path that addresses nothing, `422`
when it arrived as a field in a request body.

`rate_limit_exceeded` is the one error that carries a `Retry-After` header, in
whole seconds. Its `details` name which limit tripped and how long its window is;
see [Rate limits](#rate-limits).

Both `401`s carry `WWW-Authenticate: Bearer`. They are deliberately `401` and
not `403`: a `403` says the caller is known and not permitted, which needs a
permission model this build does not have. `invalid_api_key` is one answer for
malformed, unknown and revoked credentials alike, so that a guesser cannot sort
their attempts into "nearly right" and "wrong". The credential a request sent is
never echoed back in an error.

## Configuration

All settings are read from `FORMS_`-prefixed environment variables, or from a
`.env` file in the working directory.

| Variable                       | Default    | Meaning                                 |
| ------------------------------ | ---------- | --------------------------------------- |
| `FORMS_DATABASE_URL`           | *required* | SQLAlchemy database URL                 |
| `FORMS_MAX_BODY_BYTES`         | `262144`   | Largest accepted request body, in bytes |
| `FORMS_MAX_FIELDS`             | `100`      | Largest number of name/value pairs      |
| `FORMS_MAX_FIELD_NAME_LENGTH`  | `128`      | Largest field name, in characters       |
| `FORMS_MAX_FIELD_VALUE_LENGTH` | `16384`    | Largest field value, in characters      |
| `FORMS_WEBHOOK_CONNECT_TIMEOUT_SECONDS` | `5`  | Wait for a webhook to accept a connection |
| `FORMS_WEBHOOK_READ_TIMEOUT_SECONDS`    | `10` | Wait for a webhook to respond             |
| `FORMS_ALLOW_PRIVATE_WEBHOOK_TARGETS`   | `false` | Permit loopback and private webhook targets. Development only |
| `FORMS_WEBHOOK_MAX_ATTEMPTS`            | `5`     | Attempts before a delivery is given up on |
| `FORMS_WEBHOOK_RETRY_INITIAL_SECONDS`   | `10`    | Wait before the second attempt; later waits double |
| `FORMS_WEBHOOK_RETRY_MAX_SECONDS`       | `3600`  | Cap on the wait between attempts           |
| `FORMS_WORKER_POLL_SECONDS`             | `1`     | How often an idle worker looks for work    |
| `FORMS_WORKER_BATCH_SIZE`               | `10`    | Deliveries a worker claims at once         |
| `FORMS_WORKER_LEASE_SECONDS`            | `60`    | How long a worker's claim holds            |
| `FORMS_RATE_LIMIT_ENABLED`              | `true`  | Enforce the public ingestion rate limits   |
| `FORMS_RATE_LIMIT_IP_REQUESTS`          | `60`    | Attempts one source address may make per window |
| `FORMS_RATE_LIMIT_IP_WINDOW_SECONDS`    | `60`    | How long the per-address window lasts      |
| `FORMS_RATE_LIMIT_ENDPOINT_REQUESTS`    | `600`   | Attempts one endpoint may receive per window |
| `FORMS_RATE_LIMIT_ENDPOINT_WINDOW_SECONDS` | `60` | How long the per-endpoint window lasts     |
| `FORMS_RATE_LIMIT_IP_SECRET`            | unset   | Secret keying the digest addresses are counted under |
| `FORMS_TRUSTED_PROXY_HOPS`              | `0`     | Reverse proxies of your own in front of this process |

The rate limit settings apply only to `POST /f/{endpoint_id}`; see
[Rate limits](#rate-limits). `FORMS_TRUSTED_PROXY_HOPS` is security-sensitive:
leaving it at `0` behind a proxy makes every visitor share one bucket, and
setting it higher than your real chain lets clients pick their own.

There is deliberately no setting for a management API key. Keys live in the
database so that creating and revoking one needs no restart, and so that a
revoked key stops working on the very next request. See
[Management API keys](#management-api-keys).

## Development

```bash
pytest                    # run the test suite
ruff check .              # lint
ruff format --check .     # formatting check
mypy                      # type check
```

### Two test layers

Most tests run against an in-memory SQLite database, one per test, so `pytest`
needs no services and leaves nothing behind. Their schema is built from the
models and stamped as migrated, rather than replayed migration by migration,
because doing that a few hundred times would cost far more than it proves.

A smaller suite under `tests/integration/` runs against a real PostgreSQL
database, for the things SQLite cannot model honestly: `SELECT ... FOR UPDATE
SKIP LOCKED`, real constraint enforcement, and genuinely concurrent worker
sessions. It skips itself unless you point it at a database it may destroy:

```bash
export HYMICAL_TEST_POSTGRES_URL=postgresql+psycopg://forms:forms@localhost:5432/forms_test
pytest tests/integration -m postgres
```

One of those tests asserts that the migrations and the models describe the same
schema, which is what keeps the fast suite's shortcut honest. Others upgrade a
PostgreSQL database that already holds endpoints, submissions, deliveries and
attempts, and check that the data is exactly what it was afterwards, that the
downgrade removes only what the newer revision added, and that the data survives
that too. Another settles the manual replay race: several real connections
replay one failed delivery at the same instant, and exactly one of them wins.

The rate limit suite there is the one that could not be faked. It builds several
whole applications, each with its own engine and connection pool, and has them
submit at the same instant against one database. Exactly the configured number of
attempts is accepted and the rest are refused, no increment is lost, and a budget
one application spent is already spent for another that has never seen the client
before, which is what "shared enforcement rather than process-local state"
actually has to mean.

CI runs the lint, format and type checks once, the fast suite across Python
3.11 to 3.13, and the PostgreSQL suite once against a PostgreSQL 17 service.

### Layout

```
src/hymical_forms/
  app.py            application assembly and startup
  apikeys.py        management key rules: format, generation, digesting
  cli.py            the operator command line for management keys
  config.py         typed settings
  db.py             engine and session lifecycle
  errors.py         the shared JSON error envelope
  delivery.py       the outbound webhook request itself
  ingestion.py      domain rules: endpoint IDs, submission validation
  middleware.py     request body size limit
  models.py         the persisted schema
  ratelimit.py      rate limit rules: windows, subjects, client address trust
  storage.py        queries and writes
  webhooks.py       webhook rules: URL validation, payload, signature, retry policy
  worker.py         the delivery worker process
  schema.py         the boundary between the application and Alembic
  main.py           ASGI entrypoint
  api/              HTTP routes and response models
    endpoints.py    creating, listing, inspecting and changing endpoints
    deliveries.py   reading the delivery queue and replaying a failed delivery
    submissions.py  public form ingestion
    security.py     the management authentication dependency
    pagination.py   the one cursor design both list routes share
  migrations/       Alembic environment and revisions
```

`ingestion.py`, `webhooks.py`, `apikeys.py` and `ratelimit.py` hold the domain
rules and know nothing about HTTP or the database. `models.py` and `storage.py` are the only
modules that write queries, and `delivery.py` is the only one that makes an
outbound request. `api/` translates requests into domain rules and storage calls,
and their outcomes into responses. `api/security.py` holds the one authentication
dependency every management route declares, so the rules cannot drift apart route
by route. `worker.py` and `cli.py` are separate processes and share only the
database with the API; the worker does not authenticate through the HTTP API at
all.

### Storage notes

Submission fields are stored as a JSON object mapping each field name to the
list of values submitted under it, which is how repeated names survive intact.
On PostgreSQL the column is `json` rather than `jsonb`, because `jsonb`
normalises object key order and would silently reorder a form's fields.

Each request runs in one transaction, committed explicitly rather than in the
session teardown, so a failure becomes an error response instead of a success
for a row that never landed. A failure anywhere before the commit leaves the
database untouched.

An idempotency key is unique per endpoint through a database constraint on
`(endpoint_id, idempotency_key)`. Both PostgreSQL and SQLite treat NULLs in a
unique constraint as distinct, so submissions sent without a key stay
unrestricted without needing a partial index. A lookup before inserting is only
an optimisation for the common retry; when two requests race, one insert loses
on the constraint, rolls back and reads the winner's row. A `CHECK` constraint
keeps the key and its fingerprint either both set or both absent.

A submission and the delivery it owes are written in one transaction. Either
both land or neither does, so there is no state in which a form was accepted but
the promise to deliver it went missing, and none in which delivery work exists
for a submission that does not.

The network call happens later, in the worker, with no transaction open. Holding
a database transaction across a call to somebody else's server would tie the
connection pool to how fast that server answers.

Workers claim deliveries with `SELECT ... FOR UPDATE SKIP LOCKED` on PostgreSQL,
so two workers scanning at once are handed different rows rather than fighting
over the same one. SQLite has no such locking and silently ignores `FOR UPDATE`,
so the claim also performs a conditional update and treats a row as claimed only
if that update matched. That guard is redundant under `SKIP LOCKED` and is what
makes the claim safe on SQLite.

This is covered by real integration tests: concurrent PostgreSQL sessions claim
disjoint work, a row another worker holds is skipped rather than waited on, and
an expired lease becomes reclaimable by exactly one worker.

Manual replay is settled the same way, by the database rather than by the
application: one conditional `UPDATE` moves a delivery out of `failed`, and a
second simultaneous request matches no row and is refused. Reading the state,
judging it in Python and then writing it would let both requests pass the check
and both reset the retry cycle, which is the duplicated work this exists to
prevent.

Rate limit counters are the one table here that records nothing durable. A row is
a limiter, a subject and a window start, all three of which are the primary key,
so the index the key already creates is the index the increment conflicts on and
there is no second structure to keep in agreement with it. The increment is a
single `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`, committed on its own
rather than inside the submission's transaction, so a submission that is refused
or fails to store cannot roll back the accounting that refused it. Every row
stops being consulted the moment its window ends, which is what makes bulk
deletion of old windows safe and why the whole table can be lost for the price of
one window of accounting.

The two attempt counters on a delivery are what let a replay be both honest and
useful. `attempts` is the lifetime total and only ever rises, so it can number
the audit trail without a number ever being reused. `cycle_attempts` is the
count since the delivery last entered the queue, so the retry policy has
something to measure that a replay is allowed to reset. Before anything is
replayed the two are the same number, which is why upgrading an existing
database sets one from the other.

## Limitations

- **Delivery is at-least-once, never exactly-once.** A worker that delivers
  successfully and dies before recording it will have its lease expire, and the
  next worker will deliver the same event again. Deduplicate on the submission
  `id` in the signed payload.
- **There is no user, account or role model.** A management key administers the
  whole service. Keys cannot be scoped to an endpoint, a tenant or a permission,
  and every valid key can do everything a management key can do. Separate keys
  are useful for revoking one caller without disturbing another, and for nothing
  else yet.
- **A failed delivery is never retried on its own.** It stays `failed` until an
  operator replays it, and nothing notices that it failed for you: there is no
  alerting, no dead-letter notification and no automatic sweep.
- **A concurrent `PATCH` on one endpoint is last-write-wins.** The row is locked
  for the transaction on PostgreSQL, so two operators rotating a signing secret
  at the same moment are serialised and both get an answer that matches what was
  stored. SQLite has no such locking and is not a production target.
- **The lease must outlast a delivery attempt.** A batch is delivered
  concurrently, so it takes about as long as its slowest single delivery rather
  than the sum, but if `FORMS_WORKER_LEASE_SECONDS` were set below the connect
  and read timeouts combined, another worker could claim a delivery that is still
  in flight and send it twice. The defaults leave a wide margin; keep it that way
  if you change them.
- **SSRF protection is partial.** Destination URLs are checked for scheme and for
  literal internal addresses, and redirects are not followed. Hostnames are
  **not** resolved, so a name that resolves to a private address still passes,
  and DNS rebinding is not addressed at all. Closing this properly means
  resolving at request time and pinning the connection to the validated address.
  Treat the current checks as a guardrail against mistakes, not a defence against
  an attacker who can configure endpoints. Authentication narrows who that is to
  whoever holds a management key; it does not make the checks complete.
- **Rate limiting is traffic protection, not spam protection.** It bounds how
  much a source or an endpoint can send; it has no opinion whatsoever about what
  is in a submission. There is no CAPTCHA, no Turnstile, no content or ML
  classification, no honeypot field, no disposable-email detection and no email
  verification, so a public deployment still accepts junk up to the configured
  rate. Form ingestion is public by design and stays that way.
- **The rate limit windows are fixed, so the boundary is soft.** A client that
  spends a whole window just before it ends and another just after can make twice
  the configured requests across those two windows. Set the window shorter if
  that burst matters more to you than the smaller counters a longer window keeps.
- **The client address is only as trustworthy as your deployment.** Behind a
  reverse proxy, `FORMS_TRUSTED_PROXY_HOPS` must match your real chain. Left at
  `0` every visitor shares your proxy's bucket, and set too high a client can
  forge `X-Forwarded-For` entries and pick its own. The default trusts nothing
  but the socket peer, which is the safe end to fail towards but is wrong behind
  a proxy.
- **Rate limiting adds writes to the ingestion path.** Every public attempt costs
  one upsert per limiter, committed before the body is parsed. That is the price
  of a limit that is shared across processes rather than enforced per process,
  and it means the limiter fails closed: if the database is unreachable, the
  attempt is refused with `503` rather than let through uncounted.
- **A lost management key cannot be recovered,** only replaced. The server holds
  a digest and nothing else. Create a new key, move your callers onto it, and
  revoke the old one by the key ID `list-keys` still shows.
- **`last_used_at` is written on every authenticated management request.** That
  is one extra small write per request, which management traffic is rare enough
  to absorb and the ingestion path never pays because it is not authenticated. A
  failure to write it is logged and ignored rather than allowed to turn a valid
  credential into a `401`.
- **A signing secret cannot be rotated in place.** Rotation happens only as a
  side effect of changing the destination, so re-keying a receiver that stays at
  the same URL means pointing the endpoint elsewhere and back, or standing up a
  second URL. A dedicated rotate action is not implemented.
- **Migrations are applied by hand, one command at a time.** There is no
  zero-downtime story and none is claimed: a migration that rewrites a table
  will lock it, and a build whose expected revision does not match the database
  refuses to start rather than serving against a schema it does not understand.
  Plan a deploy as migrate-then-restart.
- **No way to read submissions back over the API.** They are stored, and a
  delivery can be inspected, but the submitted values themselves are deliberately
  not exposed by any route. Retrieval, export and retention are not implemented.
- **Endpoints cannot be deleted, and an endpoint ID cannot be changed.**
  Disabling an endpoint is the way to stop it accepting submissions.
- **No file uploads.** Multipart text fields are accepted; file parts are
  rejected.
- **`multipart/form-data` bodies are buffered in memory,** bounded by
  `FORMS_MAX_BODY_BYTES`.
- A rejected submission reveals whether an endpoint ID exists, which allows
  enumeration. This is unavoidable while form ingestion is public, and it stays
  true now that endpoint creation is not.
- **Idempotency keys never expire.** A key stays spent for as long as its
  submission is stored, so the table only grows. Expiry belongs with retention.
- **Idempotency keys are shared across all clients of an endpoint,** because
  there is nothing to scope them to yet. Guessing another client's key returns
  that submission's ID and timestamp, though never its contents. Random keys of
  the required length make this impractical. Management API keys do not close
  this: they authenticate whoever administers the endpoint, not the visitors
  submitting the form, and the submission route stays public by design.
- **A replay is only recognised once the first attempt has committed.** A retry
  sent while the original is still in flight is treated as a concurrent request,
  which is safe, but a retry sent after the original *failed* is a new
  submission, which is correct.
- Submission IDs are opaque and not yet guaranteed stable in format.

## License

[Apache License 2.0](LICENSE).

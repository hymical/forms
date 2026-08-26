# Limitations

An honest list of what this build does not do. It is worth reading before you
deploy it, and it is kept complete rather than flattering.

## Delivery

- **Delivery is at-least-once, never exactly-once.** A worker that delivers
  successfully and dies before recording it will have its lease expire, and the
  next worker will deliver the same event again. Deduplicate on the submission
  `id` in the signed payload. See
  [Delivery semantics](../architecture/delivery-semantics.md).
- **A failed delivery is never retried on its own.** It stays `failed` until an
  operator replays it, and nothing notices that it failed for you: there is no
  alerting, no dead-letter notification and no automatic sweep.
- **The lease must outlast a delivery attempt.** A batch is delivered
  concurrently, so it takes about as long as its slowest single delivery rather
  than the sum. But if `FORMS_WORKER_LEASE_SECONDS` were set below the connect and
  read timeouts combined, another worker could claim a delivery that is still in
  flight and send it twice. The defaults leave a wide margin; keep it that way if
  you change them. The overtaken worker cannot overwrite the newer worker's state,
  and logs a warning when it finds it has lost the claim, but the duplicate
  request has already gone out by then.
- **A signing secret cannot be rotated in place.** Rotation happens only as a side
  effect of changing the destination, so re-keying a receiver that stays at the
  same URL means pointing the endpoint elsewhere and back, or standing up a
  second URL. A dedicated rotate action is not implemented.

## Security

- **SSRF protection is partial.** Destination URLs are checked for scheme and for
  literal internal addresses, and redirects are not followed. Hostnames are
  **not** resolved, so a name that resolves to a private address still passes, and
  DNS rebinding is not addressed at all. Closing this properly means resolving at
  request time and pinning the connection to the validated address. Treat the
  current checks as a guardrail against mistakes, not a defence against an
  attacker who can configure endpoints. Authentication narrows who that is to
  whoever holds a management key; it does not make the checks complete.
- **There is no user, account or role model.** A management key administers the
  whole service. Keys cannot be scoped to an endpoint, a tenant or a permission,
  and every valid key can do everything a management key can do. Separate keys are
  useful for revoking one caller without disturbing another, and for nothing else
  yet.
- **A lost management key cannot be recovered,** only replaced. The server holds a
  digest and nothing else. Create a new key, move your callers onto it, and revoke
  the old one by the key ID `list-keys` still shows.
- **A rejected submission reveals whether an endpoint ID exists,** which allows
  enumeration. This is unavoidable while form ingestion is public, and it stays
  true now that endpoint creation is not.

## Rate limiting and abuse

- **Rate limiting is traffic protection, not spam protection.** It bounds how much
  a source or an endpoint can send; it has no opinion whatsoever about what is in a
  submission. There is no CAPTCHA, no Turnstile, no content or ML classification,
  no honeypot field, no disposable-email detection and no email verification, so a
  public deployment still accepts junk up to the configured rate. Form ingestion is
  public by design and stays that way.
- **The rate limit windows are fixed, so the boundary is soft.** A client that
  spends a whole window just before it ends and another just after can make twice
  the configured requests across those two windows. Set the window shorter if that
  burst matters more to you than the smaller counters a longer window keeps.
- **The client address is only as trustworthy as your deployment.** Behind a
  reverse proxy, `FORMS_TRUSTED_PROXY_HOPS` must match your real chain. Left at
  `0` every visitor shares your proxy's bucket, and set too high a client can forge
  `X-Forwarded-For` entries and pick its own. The default trusts nothing but the
  socket peer, which is the safe end to fail towards but is wrong behind a proxy.
  See [Reverse proxy](../operations/reverse-proxy.md).
- **Rate limiting adds writes to the ingestion path.** Every public attempt costs
  one upsert per limiter, committed before the body is parsed. That is the price of
  a limit that is shared across processes rather than enforced per process, and it
  means the limiter fails closed: if the database is unreachable, the attempt is
  refused with `503` rather than let through uncounted.

## Idempotency

- **Idempotency keys expire only with their submission.** A key stays spent for as
  long as its submission is stored. Configuring
  [retention](../operations/retention.md) is what eventually releases one, because
  the uniqueness constraint lives on the row; without retention the table only
  grows.
- **Idempotency keys are shared across all clients of an endpoint,** because there
  is nothing to scope them to yet. Guessing another client's key returns that
  submission's ID and timestamp, though never its contents. Random keys of the
  required length make this impractical. Management API keys do not close this:
  they authenticate whoever administers the endpoint, not the visitors submitting
  the form, and the submission route stays public by design.
- **A replay is only recognised once the first attempt has committed.** A retry
  sent while the original is still in flight is treated as a concurrent request,
  which is safe, but a retry sent after the original *failed* is a new submission,
  which is correct.

## Data and API surface

- **Submissions cannot be searched by content.** The listing filters by endpoint
  and by received time and nothing else. There is no field-value search, no
  full-text search, and no filtering by delivery state. Export the range and grep
  it. See [Browsing submissions](../guides/submission-management.md).
- **There is no per-submission delete route.** Removing one person's data means
  finding their submissions through the listing filters and deleting the rows
  directly in the database. Retention deletes by age, not by who sent something.
- **A failed delivery pins its submission indefinitely.** Retention will not
  delete a submission whose delivery could still be replayed, because a replay
  rebuilds the payload from it. Resolve failed deliveries if you want retention to
  reach them. See [Retention](../operations/retention.md).
- **Delivery records are never deleted.** Retention covers submissions only, so
  the delivery and attempt tables grow without bound. Deleting a submission
  unlinks its delivery rather than removing it.
- **Retention is never automatic.** Nothing is deleted until an operator runs
  `cleanup-submissions`. There is no scheduler and no daemon; use cron.
- **An export is capped and refused rather than truncated,** at
  `FORMS_EXPORT_MAX_SUBMISSIONS`. Larger sets have to be taken in ranges. There is
  no background export job and no download that resumes.
- **A CSV export is built in one pass before it is sent,** because its header is
  the union of every field name in the export. The size cap is what bounds that.
  JSON exports stream.
- **Endpoints cannot be deleted, and an endpoint ID cannot be changed.** Disabling
  an endpoint is the way to stop it accepting submissions.
- **No file uploads.** Multipart text fields are accepted; file parts are
  rejected.
- **`multipart/form-data` bodies are buffered in memory,** bounded by
  `FORMS_MAX_BODY_BYTES`.
- **Submission IDs are opaque and not yet guaranteed stable in format.**
- **Nothing is encrypted at rest.** Submitted values are ordinary JSON in
  PostgreSQL. See [Data handling](data-handling.md).

## Operations

- **Migrations are applied by hand, one command at a time.** There is no
  zero-downtime story and none is claimed: a migration that rewrites a table will
  lock it, and a build whose expected revision does not match the database refuses
  to start rather than serving against a schema it does not understand. Plan a
  deploy as migrate-then-restart.
- **A concurrent `PATCH` on one endpoint is last-write-wins.** The row is locked
  for the transaction on PostgreSQL, so two operators rotating a signing secret at
  the same moment are serialised and both get an answer that matches what was
  stored. SQLite has no such locking and is not a production target.
- **`last_used_at` is written on every authenticated management request.** That is
  one extra small write per request, which management traffic is rare enough to
  absorb and the ingestion path never pays because it is not authenticated. A
  failure to write it is logged and ignored rather than allowed to turn a valid
  credential into a `401`.
- **SQLite is not a production target.** It backs the test suite. It has no row
  locking, which several parts of this service rely on. See
  [Concurrency](../architecture/concurrency.md).
- **A fresh SQLite database cannot be migrated to the current schema.** Revision
  `0005` alters two tables with foreign keys between them directly rather than
  through Alembic's batch mode, which is what SQLite needs to change a column at
  all, so `alembic upgrade head` reaches `0004` on SQLite and then fails. It is
  written for PostgreSQL, which does not have this restriction. The test suite is
  unaffected: it builds its SQLite schema from the models rather than migrating
  it. See [Database migrations](../operations/migrations.md#sqlite).

## Not implemented at all

Dashboards, a frontend, submission search, scheduled retention, delivery
retention, endpoint deletion, spam filtering, CAPTCHA, email verification, user
accounts, tenancy, per-endpoint or per-tenant quotas, and billing.

## Related

- [Data handling](data-handling.md) for what happens to submitted values
- [Security](../architecture/security.md) for the boundaries that do exist
- [Delivery semantics](../architecture/delivery-semantics.md)
- [Rate limiting](../guides/rate-limiting.md)

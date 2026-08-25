# Data Handling

What this service does with the contents of a submission, and what it does not.
This page describes behaviour. It is not legal or compliance advice, and no
claim of compliance with any regime is made here or anywhere else in this
documentation.

**You are responsible for the data you collect.** This service stores and
forwards what your forms ask for. What you ask for, why, what you tell people
about it, and how long you keep it are your decisions.

## Where submitted values go

A submitted field value reaches exactly four places:

| Destination | When |
| --- | --- |
| The `submissions` table | Always, on acceptance |
| Your webhook receiver | If the endpoint has a webhook, in the signed payload |
| `GET /submissions/{id}` | When an authenticated operator asks for it |
| `GET /submissions/export` | When an authenticated operator exports it |

Nowhere else. In particular:

- **No public route returns a submitted value.** `POST /f/{endpoint_id}` answers
  with an acknowledgement: an identifier, a timestamp, a count. It does not echo
  what was sent, even though the sender already has it.
- **Nothing is logged.** No field name and no field value is written to the
  application log, including by the routes that return them. What is logged about
  an export is who asked, for what endpoint filter, in what format, and how many
  rows it came to.
- **Nothing appears in an error.** An idempotency conflict says the content
  differs, never how. A rate limit refusal names the scope and the wait, never a
  subject or a payload.
- **Nothing reaches the rate limit tables.** Those hold a limiter, a subject and a
  count. The per-address subject is a digest, and no raw address is stored.
- **Delivery records carry no submitted values.** A delivery holds its
  destination, its state and its counters. Attempt records hold an outcome and a
  bounded error message. Receiver response bodies are not stored at all.

## Reading submissions back is authenticated

Every route that returns submitted values requires a management API key. There is
no unauthenticated read path and no signed-URL scheme.

A management key administers the whole service. There is no way to issue a key
that can read one endpoint's submissions and not another's. See
[Limitations](limitations.md).

## Listings are metadata

`GET /submissions` returns a count of values, never the values. Walking a busy
endpoint means fetching page after page, and each of those pages would otherwise
be a copy of somebody's form data in a proxy cache or a terminal scrollback. Ask
for the values when you want the values.

## Exports leave this service

An export is a file. Once it is downloaded, this service has no further say in
where it goes, who opens it or how long it lives. Treat one the way you would
treat a database dump.

CSV exports are additionally written so that a spreadsheet does not evaluate a
cell as a formula. See
[Exporting submissions](../guides/exporting-submissions.md#spreadsheet-formulas).

## What is never returned

| Not returned | Why |
| --- | --- |
| The payload fingerprint | An internal detail of how a retry is recognised |
| The `Idempotency-Key` | A secret in practice: it resolves to a submission through the public route |
| A webhook signing secret | Leaves the service once, in the response that generated it |
| A management credential | Only a digest is stored, so there is nothing to return |

## Deletion

Submissions are kept indefinitely unless you configure
[retention](../operations/retention.md) and run the cleanup command.

There is no per-submission delete route, no endpoint deletion and no bulk erase
by field value. Removing one person's data means finding their submissions,
which you can do with the listing filters and an export, and deleting them
directly in the database. That is a real gap; see
[Limitations](limitations.md).

Retention deletion is permanent. It also unlinks, rather than removes, the
delivery records that referenced the submission: what this service tried to do
survives, the form content does not. See
[Retention](../operations/retention.md#what-a-sweep-never-destroys).

## Transport

Nothing here encrypts anything at rest. Submitted values are stored as ordinary
JSON in PostgreSQL, readable by anyone who can read the database. Disk
encryption, database access control and backup handling are yours.

This service does not terminate TLS. Run it behind a reverse proxy that does. See
[Reverse proxy](../operations/reverse-proxy.md).

## Related

- [Retention](../operations/retention.md)
- [Exporting submissions](../guides/exporting-submissions.md)
- [Security](../architecture/security.md)
- [Limitations](limitations.md)

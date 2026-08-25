# Submission Retention

Stored submissions are kept forever unless you configure otherwise, and nothing
is ever deleted automatically. Retention is one setting and one command an
operator runs.

## Configuring it

```bash
FORMS_SUBMISSION_RETENTION_DAYS=90
```

Unset, or `0`, means keep submissions indefinitely. That is the default, and it
is the only safe thing an unconfigured value can mean: a service that started
deleting form data because nobody had set a variable would be indefensible.

Setting it makes submissions older than that age *eligible* for deletion. It does
not delete anything on its own.

## Running the sweep

```bash
python -m hymical_forms.cli cleanup-submissions --dry-run
```

```
Retention keeps submissions for 90 days, so the cutoff is 2026-05-27T12:00:00+00:00.
412 submission(s) received before then are eligible for deletion. A submission
whose delivery is still pending, processing or replayable is not eligible,
however old it is.
Dry run: nothing was deleted.
```

A dry run performs one counting query and writes nothing at all. Drop the flag to
delete:

```bash
python -m hymical_forms.cli cleanup-submissions
```

```
Deleted 412 submission(s) in batches of up to 500. Delivery records and their
attempt history were left in place.
```

The command reads `FORMS_DATABASE_URL` like every other operator command, and
checks the schema revision before it touches anything.

### Sweeping without configuring retention

```bash
python -m hymical_forms.cli cleanup-submissions --older-than-days 365
```

An explicit age overrides the configured one, which is what lets a deployment
that keeps submissions indefinitely still clear out one old range by hand.

With no retention configured and no `--older-than-days`, the command refuses:

```
No submission retention is configured, so nothing is eligible for deletion. Set
FORMS_SUBMISSION_RETENTION_DAYS, or pass --older-than-days to sweep this once
without configuring anything.
```

Refused rather than treated as "delete nothing", so an operator who expected a
sweep to happen finds out that it did not.

### Scheduling

There is no daemon, and no scheduler ships with this service. Use whatever
already runs your periodic work:

```cron
17 4 * * * cd /srv/forms && /srv/forms/.venv/bin/python -m hymical_forms.cli cleanup-submissions
```

A sweep that deletes stored form data should be something a person set up
deliberately against a database they named, not something an API process does on
the side of serving a request.

## What is eligible, and what is not

This is the part worth understanding, because age alone does not decide it.

A queued webhook delivery does **not** carry a copy of the submitted fields. The
worker loads the submission and builds the payload from it at the moment it
sends. So the submission is needed for as long as any further attempt is
possible.

A submission older than the cutoff is deleted when:

- it owes no delivery at all, because its endpoint has no webhook; **or**
- its delivery is `delivered`, and so will never be sent again.

It is kept, however old it is, when its delivery is:

| State | Why it is kept |
| --- | --- |
| `pending` | Waiting for its due time. The payload has not been sent yet |
| `processing` | A worker is holding it right now |
| `failed` | Replayable. A replay rebuilds the payload from the submission |

`failed` is the one that catches people out. A terminally failed delivery is not
finished, it is waiting for an operator to
[replay it](../guides/delivery-replay.md), and a replay reads the submission
again. Retention that took the payload would leave a delivery that can be
requeued and can never succeed. So a failed delivery protects its submission
indefinitely.

The practical consequence: if you want retention to reach those, resolve them
first. Replay the ones worth replaying, and accept the ones that are not.

## What a sweep never destroys

Deleting a submission does not delete the record of what this service did about
it. The delivery row and every attempt made for it stay exactly where they are;
the database unlinks them from the submission and leaves them standing.

After a sweep, such a delivery still reports its state, its attempt counts, its
timings, its destination and its endpoint. Its `submission_id` reads `null`. Its
attempt history is still readable through
`GET /deliveries/{delivery_id}`.

Operational history is worth more than the form content it was carrying, and this
is where that judgement is made concrete. It is why the foreign keys are
`ON DELETE SET NULL` rather than `ON DELETE CASCADE`, and why a delivery records
its own endpoint rather than reaching it through the submission.

## Batching and safety

Deletion is many short committed transactions rather than one long one. Each
batch takes at most `--batch-size` submissions, defaulting to 500, deletes them
and commits.

```bash
python -m hymical_forms.cli cleanup-submissions --batch-size 100
```

A single statement over a large backlog would hold locks on every row it touched
for as long as the whole sweep took. Batching means a busy database keeps
serving, and a run that is interrupted has still durably removed everything it
reported.

A run also stops after a fixed number of batches so that a sweep against an
enormous backlog comes back rather than running unbounded. When that happens the
command says so, and running it again continues.

## What deletion frees

Deleting a submission releases the `Idempotency-Key` it was sent with, since the
uniqueness constraint is on the row. A client retrying with that key long after
the submission has been swept creates a new submission rather than resolving to
the old one. In practice retention ages are far longer than any client's retry
window.

## Limits

- **Deletion is permanent.** There is no soft delete, no archive and no undo. Take
  an [export](../guides/exporting-submissions.md) first if you want a copy.
- **Nothing is scheduled for you.** Retention only happens when the command runs.
- **A failed delivery pins its submission indefinitely.** See above.
- **Delivery records are never removed.** They accumulate. Delivery retention is
  not implemented.

## Related

- [Exporting submissions](../guides/exporting-submissions.md) to keep a copy first
- [Delivery replay](../guides/delivery-replay.md) for resolving what is pinned
- [Data handling](../reference/data-handling.md)
- [Configuration reference](configuration-reference.md)

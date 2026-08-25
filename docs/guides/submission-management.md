# Browsing Submissions

Submissions have always been stored. Until now the only way to see one was to
receive its webhook. This guide covers reading them back through the management
API.

Every route here needs a management API key. The values somebody typed into your
form are never returned by a public route.

## Listing

```bash
curl "http://127.0.0.1:8000/submissions" \
  -H "Authorization: Bearer $HYMICAL_KEY"
```

```json
{
  "items": [
    {
      "id": "sub_48984534f33749c49a88de2d59400dce",
      "endpoint_id": "contact-form",
      "received_at": "2026-08-25T10:00:00Z",
      "field_count": 3,
      "idempotent": false,
      "delivery": {
        "id": "whd_9f2c1a7b4e8d4c3fa1b6d0e5c8a72f31",
        "state": "delivered",
        "attempt_count": 1
      }
    }
  ],
  "next_cursor": null
}
```

The listing is metadata only. `field_count` says how much the submission carried,
not what it was. A field submitted three times counts three times, the same as it
does on the ingestion response.

This is deliberate. Walking a busy endpoint means fetching page after page, and
each of those pages would otherwise be a copy of somebody's form data sitting in
a log, a proxy cache or a terminal scrollback. Ask for the values when you want
the values.

## One submission

```bash
curl "http://127.0.0.1:8000/submissions/sub_48984534f33749c49a88de2d59400dce" \
  -H "Authorization: Bearer $HYMICAL_KEY"
```

```json
{
  "id": "sub_48984534f33749c49a88de2d59400dce",
  "endpoint_id": "contact-form",
  "received_at": "2026-08-25T10:00:00Z",
  "field_count": 3,
  "idempotent": false,
  "delivery": { "id": "whd_9f2c...", "state": "delivered", "attempt_count": 1 },
  "fields": {
    "email": ["dev@example.com"],
    "topics": ["billing", "api"]
  }
}
```

Every value is a list, always. A field submitted once is a one-element list
rather than a bare string, so a consumer never has to check which of the two it
got. Repeated values keep the order they were submitted in, because a checkbox
group is what repeated field names are for and collapsing them would discard
what somebody chose.

That is the same shape a signed webhook payload carries, so an operator reading
a submission and a receiver handling it see the same thing.

## Filtering

Three filters, all optional and all combinable:

```bash
curl -G "http://127.0.0.1:8000/submissions" \
  -H "Authorization: Bearer $HYMICAL_KEY" \
  --data-urlencode "endpoint_id=contact-form" \
  --data-urlencode "received_after=2026-08-01T00:00:00Z" \
  --data-urlencode "received_before=2026-09-01T00:00:00Z"
```

Both time bounds are **exclusive**. A submission received at exactly
`2026-08-01T00:00:00Z` is not returned by either bound.

That is what makes a walk forward through time safe: take the `received_at` of
the oldest row you have seen, pass it as `received_before`, and the next request
continues rather than repeating it.

A range that cannot match anything, where `received_after` is on or later than
`received_before`, is refused with `invalid_time_range` rather than answered with
an empty page. An empty page would hide the mistake.

There is no search over field values, and none is planned for this build. See
[Limitations](../reference/limitations.md).

## Paging

Cursor paging, exactly as for endpoints and deliveries:

```bash
curl -G "http://127.0.0.1:8000/submissions" \
  -H "Authorization: Bearer $HYMICAL_KEY" \
  --data-urlencode "limit=100" \
  --data-urlencode "cursor=sub_48984534f33749c49a88de2d59400dce"
```

Ordering is newest first, and it is total: submissions received in the same
instant are separated by their identifier, so a page boundary never repeats or
skips a row. A full page always hands back a cursor, so a walk ends on one empty
page rather than on a null cursor.

Filters must stay the same across a walk. Changing one part way through moves the
boundary the cursor was taken from.

## Finding what a delivery carried

A delivery reports its `submission_id`, and a submission reports its delivery. So
a failed delivery leads to the content that failed to arrive:

```bash
curl "http://127.0.0.1:8000/deliveries?state=failed" \
  -H "Authorization: Bearer $HYMICAL_KEY"
```

Take a `submission_id` from that page and read it back. That is usually enough to
tell a receiver that was down from a payload the receiver refused.

If `submission_id` is `null`, [retention](../operations/retention.md) has removed
the submission. That only ever happens to a delivery that already succeeded.

## Related

- [Submission Management API](../api/submission-management.md) for every parameter
- [Exporting submissions](exporting-submissions.md)
- [Retention](../operations/retention.md)
- [Data handling](../reference/data-handling.md)

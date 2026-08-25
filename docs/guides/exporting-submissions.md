# Exporting Submissions

One authenticated route writes a filtered set of submissions as a downloadable
file, in JSON or in CSV.

```bash
curl -OJ -G "http://127.0.0.1:8000/submissions/export" \
  -H "Authorization: Bearer $HYMICAL_KEY" \
  --data-urlencode "endpoint_id=contact-form" \
  --data-urlencode "received_after=2026-08-01T00:00:00Z"
```

The filters are the same three the listing takes, with the same exclusive
bounds. See [Browsing submissions](submission-management.md#filtering).

## JSON

The default. One object with one key:

```json
{
  "submissions": [
    {
      "id": "sub_48984534f33749c49a88de2d59400dce",
      "endpoint_id": "contact-form",
      "received_at": "2026-08-25T10:00:00Z",
      "fields": {
        "email": ["dev@example.com"],
        "topics": ["billing", "api"]
      }
    }
  ]
}
```

`fields` is exactly what the detail route returns, and exactly what is stored.

The document is written as the rows arrive rather than built in memory first, so
an export of thousands of submissions starts sending immediately.

## CSV

```bash
curl -OJ -G "http://127.0.0.1:8000/submissions/export" \
  -H "Authorization: Bearer $HYMICAL_KEY" \
  --data-urlencode "format=csv"
```

```csv
submission_id,endpoint_id,received_at,email,topics
sub_48984534f33749c49a88de2d59400dce,contact-form,2026-08-25T10:00:00Z,"[""dev@example.com""]","[""billing"",""api""]"
```

Three fixed metadata columns come first. After them there is one column per field
name, and the set of columns is the union of every field name in the export, in
the order each was first met.

That ordering means a CSV of one endpoint's submissions comes out roughly in the
order that endpoint's form asks its questions. Sorting alphabetically would be
just as deterministic and would read worse.

### Why every value cell is a JSON array

A form field can be submitted more than once, so a cell has to be able to hold
several values. Almost every separator you might reach for, a comma, a semicolon,
a pipe, can also appear inside somebody's answer, and then the cell is ambiguous
and nothing can tell the two apart afterwards.

So a cell holds a JSON array:

| Submitted | Cell |
| --- | --- |
| `topics=billing&topics=api` | `["billing","api"]` |
| `email=dev@example.com` | `["dev@example.com"]` |
| `phone=` | `[""]` |
| field not submitted at all | empty cell |

A field submitted once is still a one-element array, so a column has one shape
rather than two. An absent field and a field submitted empty stay distinguishable,
which a shared separator could not manage.

Escaping happens twice and neither layer is hand-written: JSON quotes the values
inside the cell, and Python's `csv` module quotes the cell inside the row. A
comma, a double quote, a newline or a tab in an answer survives both and parses
back to exactly what was submitted.

### Spreadsheet formulas

A spreadsheet reads a cell beginning with `=`, `+`, `-` or `@` as a formula
rather than as text. Exports get opened in spreadsheets, and both field names and
field values are written by whoever filled the form in.

Any cell that would begin with one of those characters, or with a tab or a
carriage return, is written with a leading apostrophe. An apostrophe is what a
spreadsheet itself writes to mean "this is text", so the cell displays the
original characters and is not evaluated.

In practice a **value** cell never needs it, because every value cell is a JSON
array and so begins with `[`. A **field name** is another matter: a form is free
to call a field `=cmd()`, and that name becomes a header cell. The rule is
applied to every cell so that the property holds regardless.

Only the export representation is changed, and only by prefixing. Nothing stored
is altered, nothing is dropped, and the original characters are still there to be
read. The API's JSON responses and the webhook payload are untouched.

### Reading a CSV back

Parse the metadata columns as strings and every other cell as JSON:

```python
import csv, json

with open("hymical-submissions-2026-08-25.csv", newline="", encoding="utf-8") as handle:
    for row in csv.DictReader(handle):
        fields = {
            name: json.loads(cell)
            for name, cell in row.items()
            if name not in ("submission_id", "endpoint_id", "received_at") and cell
        }
        print(row["submission_id"], fields)
```

If you enabled the spreadsheet escaping path on a field name, strip a single
leading apostrophe from the header before matching on it.

## Size limit

An export returns the whole filtered set, so the set has to be bounded. A filter
matching more than `FORMS_EXPORT_MAX_SUBMISSIONS`, which defaults to `10000`, is
refused:

```json
{
  "error": {
    "code": "export_too_large",
    "message": "This filter matches more than 10000 submissions, ...",
    "details": { "limit": 10000 }
  }
}
```

Refused rather than truncated, on purpose. A silently shortened export is a file
somebody archives and only discovers is incomplete much later.

To export more than the limit, narrow the range and take it in parts:

```bash
for month in 01 02 03; do
  curl -OJ -G "http://127.0.0.1:8000/submissions/export" \
    -H "Authorization: Bearer $HYMICAL_KEY" \
    --data-urlencode "received_after=2026-${month}-01T00:00:00Z" \
    --data-urlencode "received_before=2026-${month}-28T00:00:00Z"
done
```

Raising `FORMS_EXPORT_MAX_SUBMISSIONS` is the other option. Bear in mind what it
buys: a CSV is built in one pass before it is sent, because a CSV header is the
union of every field name in the export and that is only known once the last row
has been read. The limit is what bounds how much that pass holds. JSON has no
such dependency and streams.

## Filenames

Both formats are sent with `Content-Disposition: attachment` and a generated
name:

```
hymical-submissions-2026-08-25.json
hymical-submissions-2026-08-25.csv
```

`curl -OJ` and every browser will use it. The date is the day the export was
requested, in UTC.

The filename is generated entirely by this service. No endpoint identifier and no
filter value goes into it, so there is no caller input in it to sanitise and
nothing that could break out of the header. It also means two exports taken on
the same day share a name; rename them, or pass `-o` yourself.

## Related

- [Submission Management API](../api/submission-management.md)
- [Browsing submissions](submission-management.md)
- [Data handling](../reference/data-handling.md)
- [Retention](../operations/retention.md)

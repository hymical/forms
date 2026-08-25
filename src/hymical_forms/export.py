"""
how an exported submission is written, in JSON and in CSV

Nothing here touches the database, HTTP or the clock. Rendering is kept apart
from fetching so that the awkward parts, which are all in the CSV, can be
reasoned about and tested on their own.

The JSON export is written incrementally, one submission at a time, so that a
response can start before the last row has been read.

The CSV is not. A CSV needs a header, the header is the union of every field name
in the export, and that union is only known once the last row has been read. So
the CSV is built in one pass over a set already bounded by the export maximum
rather than streamed, and that bound is what keeps it honest.

Field values keep the shape they are stored in. A cell holds a JSON array, so a
field submitted three times is one cell with three ordered values in it and a
field submitted once is a one-element array rather than a bare string. Nothing
has to be escaped by hand: :mod:`csv` quotes and escapes the cell, and JSON
quotes and escapes the values inside it, so a comma, a quote or a newline in
somebody's answer survives both layers intact.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

JSON_MEDIA_TYPE = "application/json"
CSV_MEDIA_TYPE = "text/csv; charset=utf-8"

# The columns every export starts with, in this order, before the form's own
# field names. They are the submission's identity rather than its content.
METADATA_COLUMNS = ("submission_id", "endpoint_id", "received_at")

FILENAME_STEM = "hymical-submissions"

# A leading one of these makes a spreadsheet treat a cell as a formula rather
# than as text. The set is the usual one, plus the two whitespace characters some
# spreadsheets skip over before deciding.
_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")

# What a value that would be read as a formula is prefixed with. An apostrophe is
# what a spreadsheet itself writes to mean "this is text", so the cell reads as
# the original characters rather than being evaluated.
_TEXT_MARKER = "'"


@dataclass(frozen=True, slots=True)
class ExportedSubmission:
    """
    one submission in the shape an export writes it
    """

    # Deliberately not the persisted row. Nothing internal can reach an export by
    # accident, because the payload fingerprint and the idempotency key have
    # nowhere to be: this type has no field for either.
    id: str
    endpoint_id: str
    received_at: datetime
    fields: Mapping[str, list[str]]


def export_filename(suffix: str, *, now: datetime) -> str:
    """
    build the filename an export is offered under
    :param suffix: the file extension, without a dot
    :param now: the instant the export was requested
    :returns: a filename such as ``hymical-submissions-2026-08-25.csv``
    """
    # Every part of this is generated here. No endpoint identifier, no filter
    # value and nothing else a caller supplied reaches the filename, so there is
    # no user input in it to sanitise and no header for one to break out of.
    return f"{FILENAME_STEM}-{now.astimezone(UTC).date().isoformat()}.{suffix}"


def content_disposition(filename: str) -> str:
    """
    build the header that offers an export as a download
    :param filename: the generated filename to offer it under
    :returns: a Content-Disposition header value
    """
    return f'attachment; filename="{filename}"'


def json_document(submissions: Iterable[ExportedSubmission]) -> Iterator[str]:
    """
    render an export as JSON, one submission at a time
    :param submissions: the submissions to write, in export order
    :returns: an iterator over the pieces of the document, in order
    """
    # One top-level object with one key, rather than a bare array, so that the
    # document has somewhere to grow a summary later without becoming a different
    # shape. Written by hand at this level and by ``json.dumps`` at every level
    # below it, which is where the escaping that matters happens.
    yield '{"submissions":['
    separator = ""
    for submission in submissions:
        yield separator + json.dumps(_as_object(submission), separators=(",", ":"))
        separator = ","
    yield "]}"


def csv_document(submissions: list[ExportedSubmission]) -> str:
    """
    render an export as CSV, including a column for every field name in it
    :param submissions: the submissions to write, in export order
    :returns: the whole CSV document
    """
    # The header is the union of the field names across the export, in the order
    # they are first met, so a CSV of one endpoint's submissions comes out in the
    # order that endpoint's form asks its questions. Sorting alphabetically would
    # be just as deterministic and would read worse.
    names = _field_names(submissions)

    buffer = io.StringIO()
    # ``\r\n`` line endings, which is what RFC 4180 asks for and what every
    # spreadsheet expects. Stated rather than left to the default so that a value
    # containing a newline is unambiguously inside its quoted cell.
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([_safe(column) for column in (*METADATA_COLUMNS, *names)])
    for submission in submissions:
        writer.writerow(_row(submission, names))
    return buffer.getvalue()


def _row(submission: ExportedSubmission, names: list[str]) -> list[str]:
    """
    render one submission as its CSV cells
    :param submission: the submission to render
    :param names: the field names the export has a column for, in column order
    :returns: the cells of one row, in column order
    """
    # A field the submission does not carry gets an empty cell, which is distinct
    # from a field it carries with an empty value: that is ``[""]``.
    cells = [submission.id, submission.endpoint_id, _timestamp(submission.received_at)]
    for name in names:
        values = submission.fields.get(name)
        cells.append(
            "" if values is None else json.dumps(values, separators=(",", ":"), ensure_ascii=False)
        )
    return [_safe(cell) for cell in cells]


def _field_names(submissions: Iterable[ExportedSubmission]) -> list[str]:
    """
    collect the field names an export needs a column for
    :param submissions: the submissions being exported
    :returns: every field name met, in the order it was first met
    """
    names: dict[str, None] = {}
    for submission in submissions:
        for name in submission.fields:
            names[name] = None
    return list(names)


def _as_object(submission: ExportedSubmission) -> dict[str, object]:
    """
    render one submission as the object a JSON export carries
    :param submission: the submission to render
    :returns: the object to serialize
    """
    return {
        "id": submission.id,
        "endpoint_id": submission.endpoint_id,
        "received_at": _timestamp(submission.received_at),
        # Repeated values stay a list, and a single value stays a one-element
        # list, exactly as stored and exactly as a webhook payload carries them.
        "fields": {name: list(values) for name, values in submission.fields.items()},
    }


def _safe(cell: str) -> str:
    """
    keep a spreadsheet from reading an exported value as a formula
    :param cell: the cell contents as they would otherwise be written
    :returns: the cell, prefixed with a text marker if it would be evaluated
    """
    # Only the export representation is changed, and only by prefixing. The
    # stored value is untouched, and nothing is dropped or rewritten, so the
    # original characters are still there to be read.
    #
    # In practice a field value cell never needs this, because every one of them
    # is a JSON array and so begins with a bracket. A field *name* is another
    # matter: a form is free to call a field ``=cmd()``, and that name becomes a
    # header cell. Applying the rule to every cell means the property holds
    # whatever a later change does to the encoding.
    return _TEXT_MARKER + cell if cell.startswith(_FORMULA_LEADERS) else cell


def _timestamp(moment: datetime) -> str:
    """
    render a timestamp the way the rest of this service renders timestamps
    :param moment: the instant to render
    :returns: an RFC 3339 timestamp in UTC, ending in Z
    """
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")

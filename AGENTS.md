# Agent Instructions

Canonical repository instructions for any coding agent (Claude Code, Codex, or
otherwise). Read this before changing code. It states durable invariants, not
current task state.

## Project

Hymical Forms is a self-hostable service that accepts HTML form submissions
over HTTP, validates and stores them, and delivers them to a webhook with an
HMAC signature and a bounded retry schedule. It is preparing its first public
release and is not yet production-hardened; treat it as a maturing codebase
with real architectural guarantees, not a prototype.

## Architecture

These are current, load-bearing properties of the system. Preserve them unless
you have a specific, discussed reason to change one, and update the docs in
the same change if you do.

- FastAPI (`app.py`, `api/`) handles all HTTP. Nothing outside `api/` should
  know about requests or responses.
- Two security boundaries exist: **public** (`POST /f/{endpoint_id}`,
  `GET /health`, no credential, reachable from a raw HTML form) and
  **management** (everything else, a `hym_live_...` bearer key). Do not blur
  them, and do not add a new route without deciding which side it is on.
- PostgreSQL is the only supported production database. SQLite backs the fast
  test suite and local experimentation only.
- All persistence goes through `models.py` (schema) and `storage.py`
  (queries). No other module issues SQL.
- Alembic owns schema evolution. The API, the worker and the CLI never create
  or alter a table; each checks on startup that the database is at the
  revision the build expects and refuses to serve otherwise (`schema.py`).
- A submission and the obligation to deliver it are written in one database
  transaction (the transactional outbox). The API never makes an outbound
  HTTP request; `delivery.py` is the only module that does, and only the
  worker calls it.
- The worker (`worker.py`) claims due deliveries from PostgreSQL (`SELECT ...
  FOR UPDATE SKIP LOCKED` where supported) and sends them. PostgreSQL is the
  queue: there is no broker.
- Delivery is at-least-once, never exactly-once. Do not add logic that assumes
  a webhook receiver sees an event only once.
- The webhook payload shape and the `Hymical-Signature: v1=<hex hmac-sha256>`
  header are a public contract (`webhooks.py`: `build_payload`,
  `serialize_payload`, `sign`). Changing either breaks every existing
  receiver; if you must, version it.
- Idempotency (`Idempotency-Key` header) is enforced by a database unique
  constraint on `(endpoint_id, idempotency_key)`, not by an in-process check.
  The lookup-then-insert race is resolved by catching the constraint
  violation, not by locking ahead of time.
- Public-ingestion rate limiting is enforced by an atomic database upsert
  (`storage.consume_rate_limit`), shared across every API process. It is not
  per-process, in-memory, or best-effort.
- Submitted field values (form data) are sensitive. They are never logged.
  Only the authenticated submission-detail and export routes return them.
- Retention deletion (`retention.py`) must never remove a submission whose
  delivery is `pending`, `processing`, or `failed` (replayable). Only a
  submission with no webhook, or one already `delivered`, is eligible.
- Every management route depends on the single authentication dependency in
  `api/security.py` (`ManagementKeyDep`). Do not read the `Authorization`
  header directly in a route handler.

## Code boundaries

| Module | Owns | Must not contain |
| --- | --- | --- |
| `api/health.py` | Liveness endpoint | Business logic |
| `api/submissions.py` | Public ingestion (`POST /f/{endpoint_id}`) | SQL, webhook sending |
| `api/endpoints.py` | Endpoint create/list/get/update (management) | SQL |
| `api/deliveries.py` | Delivery list/get/replay (management) | SQL, outbound HTTP |
| `api/submission_management.py` | Submission list/get/export (management) | SQL |
| `api/security.py` | The management auth dependency | Route-specific logic |
| `api/pagination.py` | The shared cursor design | Table-specific queries |
| `ingestion.py` | Endpoint ID and submission validation | HTTP, database |
| `webhooks.py` | URL/SSRF validation, payload, signing, retry policy | HTTP, database |
| `delivery.py` | The one outbound HTTP request | Retry scheduling, storage |
| `apikeys.py` | Management key format, minting, digesting | HTTP, database |
| `ratelimit.py` | Windows, subjects, client-address trust | Database, HTTP |
| `retention.py` | The retention eligibility rule | Queries |
| `export.py` | JSON/CSV rendering, formula escaping | Database, HTTP |
| `models.py` | The persisted schema (SQLAlchemy models) | Queries |
| `storage.py` | Every query and write | HTTP, domain validation |
| `schema.py` | The Alembic/app startup boundary | Migrations themselves |
| `worker.py` | The delivery process: claim, send, retry | HTTP route logic |
| `cli.py` | Operator commands: keys, retention cleanup | HTTP |
| `config.py` | Typed settings from `FORMS_*` env vars | Defaults used nowhere |
| `errors.py` | The shared JSON error envelope | Route-specific messages |

If you find yourself writing SQL in `api/`, or importing FastAPI into
`ingestion.py`, `webhooks.py`, `ratelimit.py`, or `apikeys.py`, stop and move
it to the right layer instead.

## Database and migrations

- Never edit a migration that has already been merged. Create a new Alembic
  revision (`alembic revision --autogenerate -m "what changed"`) and read what
  it produces before committing it.
- A migration must not import application code. Write out the SQLAlchemy type
  directly (for example `sa.DateTime(timezone=True)`, not the app's
  `UtcDateTime` decorator) so the migration stays a frozen record.
- Every constraint and index needs an explicit name (the naming convention in
  `models.py` gives you one) so a later migration can reference it.
- Migration/model drift must stay at zero. A PostgreSQL integration test
  asserts the migrations and the models describe the same schema
  (`compare_metadata`); any schema change must keep it passing.
- A schema change needs PostgreSQL integration coverage under
  `tests/integration/`, not just the fast SQLite suite, for anything touching
  locking, constraints, or a migration.
- **SQLite migration caveat**: revisions through `0004` replay against SQLite
  in Alembic batch mode. Revision `0005` does not (it alters two
  mutually-referencing tables directly) and a fresh SQLite database cannot
  reach `head` via `alembic upgrade head`. This is documented, not a bug to
  silently work around; do not assume `alembic upgrade head` works on SQLite
  when writing docs or scripts. PostgreSQL is unaffected. The fast test suite
  builds its SQLite schema from the models with `create_all` instead of
  replaying migrations.

## Security invariants

- A management API key is 256 random bits, prefixed `hym_live_`, and stored
  **only** as a SHA-256 digest. It is shown once, at creation, by the CLI, and
  never over HTTP.
- Digest comparison uses `hmac.compare_digest`. Malformed, unknown, and
  revoked keys all produce the same `401 invalid_api_key`, with no detail
  about which.
- A webhook signing secret (`whsec_...`) is server-generated and returned only
  once, in the response of the mutation that created it.
- Outbound webhook bodies are signed HMAC-SHA256 over the exact transmitted
  bytes (`Hymical-Signature: v1=<hex>`).
- Webhook destinations must be `http`/`https` and must not be a literal
  loopback, private, link-local, multicast, reserved, or unspecified address.
  Hostnames are **not** resolved, so this is a guardrail against mistakes, not
  a complete SSRF defense. Do not describe it as more than that.
- Client addresses used for rate limiting are stored as a SHA-256 digest
  (optionally HMAC-keyed by `FORMS_RATE_LIMIT_IP_SECRET`), never raw.
- Submitted form field values are never logged. The only routes that return
  them are the authenticated submission-detail and export routes.
- CSV exports escape formula-injection leaders (`= + - @` and leading tab/CR)
  with a text marker; do not remove this when touching `export.py`.
- Error responses never leak database/driver internals, stack traces, or file
  paths. A storage failure is an opaque `503`; an unhandled exception is an
  opaque `500`.
- `POST /f/{endpoint_id}` and `GET /health` are the only unauthenticated
  routes. Every other route requires a valid management key.

## Repository style

Docstrings follow this exact shape:

```python
def example_function(value):
    """
    description of function
    :param value: description of parameter
    :returns: description of return value
    """
```

- Descriptions start lowercase and carry no trailing punctuation.
- Use `:param name:`, `:returns:`, and `:raises:` when a function can raise
  something the caller should know about.
- Never use `:return:`, `:rtype:`, or `:arg:`.
- A test whose name already says what it proves does not need a docstring.
  Do not add one just to have one.
- No em dashes anywhere in the repository: not in code, comments, docstrings,
  Markdown, YAML, TOML, migrations, tests, or error messages.
- Comments explain *why*, not what the code already says.

## Verification

```bash
pytest                    # fast suite, in-memory SQLite, no services needed
ruff check .               # lint
ruff format --check .      # formatting check
mypy                       # strict type check, over src and tests
```

PostgreSQL integration suite (needs a real, disposable database):

```bash
export HYMICAL_TEST_POSTGRES_URL=postgresql+psycopg://forms:forms@localhost:5432/forms_test
pytest tests/integration -m postgres
```

Documentation, only if you touched `docs/` or `mkdocs.yml`:

```bash
pip install -e ".[docs]"
mkdocs build --strict
```

All of the above run in CI. Run the ones relevant to what you changed; you do
not need to run the PostgreSQL suite for a documentation-only change, and you
do not need `mkdocs build` for a code-only change.

## Working rules

- Inspect the existing implementation before modifying it. Do not assume; grep
  and read.
- Prefer extending an established pattern over introducing a new abstraction.
- Do not refactor unrelated working code while making a change.
- Do not weaken or delete a test to make a change pass. Fix the change.
- Add a test for behavior that needs coverage, not to inflate a count.
- Keep documentation synchronized with behavior. A stale doc is a bug.
- Report limitations honestly. Do not call something production-ready or
  complete when it is not.
- Never commit, push, merge, tag, release, or publish unless explicitly
  instructed to in the current conversation.

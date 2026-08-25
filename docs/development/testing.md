# Testing

```bash
pytest                    # run the test suite
ruff check .              # lint
ruff format --check .     # formatting check
mypy                      # type check
```

## Two test layers

### The fast suite

Most tests run against an **in-memory SQLite database, one per test**, so
`pytest` needs no services and leaves nothing behind.

Their schema is built from the models with `create_all` and stamped as migrated,
rather than replayed migration by migration, because doing that a few hundred
times would cost far more than it proves. What keeps that shortcut honest is a
PostgreSQL test asserting that `create_all` and the migrations produce the same
schema.

Tests build their own application instances so limits can be lowered to values
that are cheap to exercise, and so a developer's local environment can never
change a test's outcome. An autouse fixture strips every `FORMS_*` variable from
the environment.

### The PostgreSQL suite

A smaller suite under `tests/integration/` runs against a real PostgreSQL
database, for the things SQLite cannot model honestly: `SELECT ... FOR UPDATE
SKIP LOCKED`, real constraint enforcement, and genuinely concurrent sessions.

It skips itself unless you point it at a database it may destroy:

```bash
export HYMICAL_TEST_POSTGRES_URL=postgresql+psycopg://forms:forms@localhost:5432/forms_test
pytest tests/integration -m postgres
```

A container is the easiest way to get one:

```bash
docker run -d --name hymical-pg -e POSTGRES_USER=forms -e POSTGRES_PASSWORD=forms \
  -e POSTGRES_DB=forms -p 5432:5432 postgres:17
```

## What the PostgreSQL suite proves

**Schema drift.** One test asserts that the migrations and the models describe
the same schema, using Alembic's own `compare_metadata`. Drift fails the build
rather than surfacing in production.

**Populated migrations.** Others upgrade a database that already holds endpoints,
submissions, deliveries and attempts, and check that the data is exactly what it
was afterwards, that a downgrade removes only what the newer revision added, and
that the data survives the round trip. Each of those tests creates and drops a
database of its own, so migrating from genuinely nothing is what is being tested.

**Worker claiming.** Six real sessions claiming at once partition the work and
never share it. A row another worker holds is skipped rather than waited on. An
expired lease is reclaimed by exactly one of two racing workers.

**Manual replay.** Several real connections replay one failed delivery at the same
instant, and exactly one of them wins.

**Rate limiting.** This is the one that could not be faked. It builds several
whole applications, each with its own engine and connection pool, and has them
submit at the same instant against one database:

- exactly the configured number of attempts is accepted and the rest are refused
- no increment is lost, so the counter matches the number of attempts exactly
- a budget one application spent is already spent for another that has never seen
  the client before

That last one is what "shared enforcement rather than process-local state" has to
mean, and it is the reason the counters live in the database. See
[Concurrency](../architecture/concurrency.md).

## Continuous integration

CI runs:

| Job | What |
| --- | --- |
| Lint, format and types | `ruff check`, `ruff format --check`, `mypy` once |
| Fast tests | `pytest` across Python 3.11, 3.12 and 3.13 |
| PostgreSQL integration | `pytest tests/integration -m postgres` against a PostgreSQL 17 service |

The PostgreSQL suite skips itself in the fast-test jobs, because no
`HYMICAL_TEST_POSTGRES_URL` is set there, which is exactly what happens on a
developer's machine.

A separate workflow builds this documentation site with `mkdocs build --strict`
on every pull request that touches it, so a broken internal link fails CI.

## Writing tests

- **Test names are the documentation.** A test whose name says what it proves
  does not need a docstring; one that needs a "why" gets one.
- **Nothing is mocked that can be exercised for real.** The webhook tests run a
  real local HTTP server. The concurrency tests use real connections.
- **Assert on the database, not just the response**, when the claim is about what
  was stored.

## Related

- [Contributing](contributing.md) for style rules and the local setup
- [Database migrations](../operations/migrations.md) for the migration workflow
- [Concurrency](../architecture/concurrency.md) for what the races are

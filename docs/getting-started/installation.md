# Installation

## Requirements

- Python 3.11 or newer
- PostgreSQL, which is the intended production database

SQLite is supported for local experimentation and backs the test suite. It is not
a supported production target: it has no row locking, which several parts of this
service rely on. See [Concurrency](../architecture/concurrency.md) for what that
changes.

## Install the package

```bash
git clone https://github.com/hymical/forms.git
cd forms
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

On Windows, activate with `.venv\Scripts\activate` instead.

The `dev` extra adds the test and lint tooling. For a deployment that only needs
to run the service, `pip install -e .` is enough.

To build this documentation site locally, install the `docs` extra instead:

```bash
pip install -e ".[docs]"
mkdocs serve
```

## Choose a database

`FORMS_DATABASE_URL` is required and has no default.

=== "PostgreSQL"

    ```bash
    createdb forms
    export FORMS_DATABASE_URL=postgresql+psycopg://forms:forms@localhost:5432/forms
    ```

=== "SQLite"

    ```bash
    export FORMS_DATABASE_URL=sqlite:///./forms.db
    ```

    Fine for trying the service out. Not a production target.

The PostgreSQL driver (`psycopg`) is a runtime dependency, so nothing extra needs
installing for either backend.

## Create the schema

Neither the API nor the worker creates or alters a table. Alembic owns the
schema, and both processes check on startup that the database is at the revision
they were built against:

```bash
alembic upgrade head
```

Migrations read the same `FORMS_DATABASE_URL` the application reads, so there is
nothing extra to configure. See [Database migrations](../operations/migrations.md)
for the full workflow.

## Next

- [Configuration](configuration.md) for the settings that matter first
- [Quick Start](quick-start.md) to get to an accepted submission

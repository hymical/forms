# Contributing

## Local setup

```bash
git clone https://github.com/hymical/forms.git
cd forms
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

On Windows, activate with `.venv\Scripts\activate` instead.

```bash
export FORMS_DATABASE_URL=sqlite:///./forms.db
alembic upgrade head
```

SQLite is enough for most development. Reach for PostgreSQL when you touch
anything to do with locking, constraints or concurrency. See
[Testing](testing.md).

## Before you open a pull request

```bash
pytest
ruff check .
ruff format --check .
mypy
```

All four run in CI. `mypy` runs in **strict** mode over `src` and `tests`.

If you touched anything under `docs/` or `mkdocs.yml`:

```bash
pip install -e ".[docs]"
mkdocs build --strict
```

Strict mode turns a broken internal link into a build failure, and CI runs the
same command.

## Style

Ruff owns formatting and linting; the configuration is in `pyproject.toml`. Line
length is 100.

### Docstrings

Every function that has or needs a docstring uses this style:

```python
def example_function(value):
    """
    description of function
    :param value: description of parameter
    :returns: description of return value
    """
```

- Descriptions start lowercase.
- No punctuation at the end of a description line.
- Use `:param name:`, `:returns:`, and `:raises:` for meaningful exceptions.
- Do not use `:return:`, `:rtype:` or `:arg:`.
- A test whose name already says what it proves does not need a docstring. One
  that needs a "why" gets one.

### No em dashes

Anywhere. Not in Python, comments, docstrings, Markdown, YAML, TOML, migrations,
tests, error messages or examples.

### Comments explain why

The code says what it does. A comment that repeats it is noise. A comment that
explains why a less obvious approach was rejected is worth keeping, and there are
a lot of those in this codebase deliberately.

## Where code goes

| Module | Holds |
| --- | --- |
| `ingestion.py`, `webhooks.py`, `apikeys.py`, `ratelimit.py` | Domain rules. No HTTP, no database |
| `models.py`, `storage.py` | The only modules that write queries |
| `delivery.py` | The only module that makes an outbound request |
| `api/` | Translating requests into domain rules and storage calls |

Do not put SQL in a route handler. Do not import HTTP concepts into a domain
module. See [Architecture overview](../architecture/overview.md).

## Adding a setting

Settings are added only when the code actually uses them. Add the field to
`config.py` with a type, a bound and a description, then document it in:

- [`.env.example`](https://github.com/hymical/forms/blob/main/.env.example)
- [Configuration reference](../operations/configuration-reference.md)

A limit with no lower bound, or a window that can be zero, is a bug waiting for
production. Give every numeric setting a constraint.

## Adding a migration

```bash
alembic revision --autogenerate -m "what changed"
```

Read what it produces. The conventions, including why a migration never imports
application code, are in [Database migrations](../operations/migrations.md).

Never edit a migration that has already been merged.

## Working on the documentation

```bash
pip install -e ".[docs]"
mkdocs serve
```

The site is built with [MkDocs](https://www.mkdocs.org/) and
[Material for MkDocs](https://squidfunk.github.io/mkdocs-material/). Pages live in
`docs/`, and the navigation is an explicit `nav:` in `mkdocs.yml`. A new page has
to be added there to appear.

Guidelines that keep the site coherent:

- **The README summarizes and links. The docs explain.** Do not copy a full
  explanation into both.
- **One explanation, one home.** Guides describe behaviour and why; API Reference
  pages are the compact route contract. Cross-link rather than repeat.
- **Do not invent functionality.** If a claim looks wrong, check it against the
  implementation before changing the page.
- Use relative links between pages, so `mkdocs build --strict` can validate them.

## Related

- [Testing](testing.md)
- [Architecture overview](../architecture/overview.md)

# Releasing

A release is a deliberate act by a maintainer, not something CI or an agent does
on its own. This page is the checklist that act follows, so that the version a
build reports, the tag it was cut from, and the release notes that describe it
all agree.

## Where the version lives

There is exactly one place the version is written down:

```python
# src/hymical_forms/__init__.py
__version__ = "0.2.0"
```

Everything else reads it. `pyproject.toml` declares the version as dynamic and
Hatch takes it from that file, and the running service reports it in three
places:

- the `GET /health` response (`version`)
- the OpenAPI document (`info.version`)
- the `User-Agent` of every outbound webhook (`Hymical-Forms/<version>`)

A tag that points at a commit whose `__version__` disagrees with it produces a
build that misreports itself in all three. That is what the checklist below is
for.

## Checklist

Work through it in order, on a release branch off an up-to-date `main`.

### 1. Decide the version

Pick the next version from what changed since the last tag:

```bash
git log --oneline "$(git describe --tags --abbrev=0)"..main
```

A fix-only release bumps the patch number. Anything that changes the webhook
payload or the `Hymical-Signature` header is a breaking change to every receiver
and must not be slipped into a patch release.

### 2. Bump the version

Edit `src/hymical_forms/__init__.py`, then update every place the documentation
quotes the version literally:

```bash
grep -rn "<old version>" docs README.md src
```

`docs/api/submissions.md` shows a sample `/health` body with the version in it.
Old release notes keep the version they describe; do not rewrite history there.

### 3. Write the release notes

Add `docs/releases/v<version>.md`, following the shape of the previous one:
what the release is, what changed, what was verified, and the limitations worth
knowing before deploying it. Then add it to the `Release Notes` section of the
`nav:` in `mkdocs.yml`, newest first, or a strict docs build will fail on an
omitted page.

If the release contains a migration, say so, name the revision, and say whether
it is safe to apply while the previous build is still running. The default answer
is no: [deploy order](../operations/deployment.md#deploy-order) is stop, migrate,
start.

### 4. Verify

Every one of these must pass on the release branch:

```bash
pytest
ruff check .
ruff format --check .
mypy
```

The PostgreSQL integration suite, against a real disposable database. A release
is never cut on the fast suite alone: it is the only suite that exercises row
locking, `SKIP LOCKED` claiming, constraint races and the migrations themselves.

```bash
export HYMICAL_TEST_POSTGRES_URL=postgresql+psycopg://forms:forms@localhost:5432/forms_test
alembic -x database_url="$HYMICAL_TEST_POSTGRES_URL" upgrade head
pytest tests/integration -m postgres
```

The documentation:

```bash
pip install -e ".[docs]"
mkdocs build --strict
```

The packages, from a clean tree, and the container image:

```bash
pip install build
python -m build
docker build -t hymical-forms:<version> .
```

Install the wheel into a fresh virtual environment and confirm it reports the
version you are about to tag:

```bash
python -c "import hymical_forms; print(hymical_forms.__version__)"
```

### 5. Merge

Open a pull request for the release branch and merge it once CI is green. CI runs
the fast suite, the PostgreSQL suite, the linters and the strict docs build, so a
green pull request repeats step 4 on a machine other than yours.

### 6. Tag the merge commit

Tag the commit that landed on `main`, not a commit on the release branch, and
check the version at that exact commit before pushing the tag:

```bash
git switch main
git pull
git show HEAD:src/hymical_forms/__init__.py | grep __version__
git tag -a v<version> -m "v<version>"
git push origin v<version>
```

The tag name is `v` followed by exactly the string in `__version__`. Use an
annotated tag (`-a`), so the tag records who cut it and when.

A pushed tag is never moved or deleted. If a tag turns out to be wrong, the fix is
a new release with a new version, not a retag: someone may already have built
from the old one.

### 7. Publish

Publishing a GitHub release, a package or an image is a separate decision from
tagging, and is made by a maintainer. Nothing in CI publishes on a tag.

## Known discrepancy: v0.2.1

The checklist above exists because the `v0.2.1` tag did not follow it.

| | `v0.2.0` | `v0.2.1` |
| --- | --- | --- |
| Commit | `8e99004` (release prep merge) | `235f8af` (delivery claim fencing merge) |
| `__version__` at that commit | `0.2.0` | `0.2.0` |
| Release notes | [v0.2.0](../releases/v0.2.0.md) | none |
| Tag type | lightweight | lightweight |

So a build from the `v0.2.1` tag reports itself as `0.2.0` in `/health`, in
OpenAPI and in its webhook `User-Agent`, and there is no page describing what
`v0.2.1` changed. What it does contain over `v0.2.0` is one change: delivery claim
fencing, which stops a worker whose lease has expired from overwriting the state
recorded by the worker that reclaimed the delivery (see
[Delivery semantics](../architecture/delivery-semantics.md#who-owns-a-claim)).

The tag has already been pushed, so it is not moved or rewritten. The next
release resolves the discrepancy by taking a version above `0.2.1`, bumping
`__version__` to it, and having its release notes cover everything since
`v0.2.0`, the fencing change included.

## Related

- [Contributing](contributing.md)
- [Testing](testing.md)
- [Deployment](../operations/deployment.md)

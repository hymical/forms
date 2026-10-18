# Deployment

Hymical Forms is two long-running processes, the API and the worker, plus a
one-shot migration step, all against one PostgreSQL database. The repository
ships a `Dockerfile` that builds one image for all three, and a `compose.yaml`
that wires them together as a working example.

!!! warning "An example, not a hardened stack"

    `compose.yaml` is a starting point to read and adapt. It does not terminate
    TLS, does not back up the database, and runs PostgreSQL in a container on the
    same host. Decide each of those for your own deployment.

## The image

```bash
docker build -t hymical-forms .
```

The image installs the package as a wheel, runs as an unprivileged user (UID
`10001`), and holds no configuration: every setting comes from `FORMS_*`
environment variables, and `.env` files are kept out of the build context. The
command decides which process a container is:

| Command | Process |
| --- | --- |
| *(default)* | The API, listening on port `8000` |
| `python -m hymical_forms.worker` | The delivery worker |
| `alembic upgrade head` | Apply migrations, then exit |
| `python -m hymical_forms.cli ...` | Operator commands: management keys, retention cleanup |

The default command starts uvicorn with `--no-proxy-headers`, so uvicorn never
rewrites the client address from `X-Forwarded-For` on its own.
`FORMS_TRUSTED_PROXY_HOPS` stays the only setting that decides whether that header
is believed. See [Reverse proxy](reverse-proxy.md).

## Running the example

```bash
export POSTGRES_PASSWORD='choose-a-real-password'
docker compose up -d --build
```

That starts, in order:

1. `postgres`, and waits until it accepts connections.
2. `migrate`, which runs `alembic upgrade head` and exits.
3. `api` and `worker`, once `migrate` has exited successfully.

The password is placed inside `FORMS_DATABASE_URL`, so if it contains a character
that is special in a URL (`@ : / % ? #`), percent-encode it or choose another.

The API is published on `127.0.0.1:8000` only, on the assumption that a reverse
proxy on the same host fronts it. PostgreSQL is not published at all.

### The first management key

Keys are minted by the operator CLI and never over HTTP. Run it as a one-off
container against the same database:

```bash
docker compose run --rm --no-deps api python -m hymical_forms.cli create-key --name admin
```

The key is printed once. Store it; it cannot be shown again. See
[Authentication](../api/authentication.md).

!!! warning "Keep `--no-deps` on every one-off command"

    The `api` service depends on `migrate`, so `docker compose run` without
    `--no-deps` runs `alembic upgrade head` before the command. After a
    `docker compose build`, that would migrate the database under the old `api`
    and `worker`, against the [deploy order](#deploy-order). With `--no-deps` the
    command only checks the schema revision, like the API and the worker do, and
    refuses to run against one it does not expect.

### Retention cleanup

Nothing is deleted automatically. Run the sweep the same way, from whatever
scheduler you already use:

```bash
docker compose run --rm --no-deps api python -m hymical_forms.cli cleanup-submissions --dry-run
docker compose run --rm --no-deps api python -m hymical_forms.cli cleanup-submissions
```

See [Submission retention](retention.md).

## Deploy order

**Stop the old processes, migrate, start the new ones.** Migrating while an old
build is still running is only safe if the change happens to be backwards
compatible, and this project does not promise that for any particular migration.
See [Database migrations](migrations.md).

A plain `docker compose up -d --build` does **not** give you that order on an
upgrade: Compose would run the new `migrate` while the old `api` and `worker` were
still serving. Do it in explicit steps instead:

```bash
docker compose build
docker compose stop api worker
docker compose run --rm migrate
docker compose up -d api worker
```

`docker compose stop` sends `SIGTERM`. The worker finishes the batch it holds,
which takes at most one attempt deadline (connect plus read timeout, 15 seconds by
default), so the example gives it a 30 second `stop_grace_period`. Keep that above
the deadline if you raise the timeouts; a worker killed mid-batch is not a
correctness problem, but its claimed deliveries are sent again once their lease
expires. See [Worker](worker.md#stopping).

Submissions sent while the API is stopped fail outright (a refused connection,
or an error from your proxy); none is accepted and then lost. There is no
zero-downtime deploy, and none is claimed.

## Health checks

`GET /health` is a **liveness** check. It reports that the API process is running
and does not touch the database, so it keeps answering while PostgreSQL is down.
That is what the example's `api` healthcheck uses, and it is the right signal for
deciding whether to restart the process.

There is **no readiness endpoint**. What exists instead:

- **At startup**, the API and the worker both verify that the database is
  reachable and at the revision the build expects, and exit if it is not. An API
  that is up and answering `/health` passed that check when it started.
- **After startup**, if the database becomes unreachable, the API keeps answering
  `/health` while every route that needs the database returns an opaque `503`. A
  load balancer that routes on `/health` will keep sending it traffic in that
  state.
- **The worker has no HTTP surface.** Its health is visible only in its logs and in
  whether due deliveries are being drained.

Leaving readiness out is a decision for v1.0, not an oversight. Every route is
deliberately either public (`POST /f/{endpoint_id}`, `GET /health`) or behind a
management key, and a database-checking route would be a new public one, so the
public surface stays at those two routes. If you need traffic withdrawn during a
database outage, have your load balancer or monitoring watch for `503` responses
from the API. See [Limitations](../reference/limitations.md#operations).

## Running without containers

The image is a convenience, not a requirement. Any process supervisor works, as
long as it:

- runs the API (`uvicorn hymical_forms.main:app`) and the worker
  (`python -m hymical_forms.worker`) as separate, independently restarted
  processes;
- stops the worker with `SIGTERM` and waits longer than one attempt deadline
  before killing it;
- runs `alembic upgrade head` as its own step, between stopping the old processes
  and starting the new ones, never from inside either process.

## Related

- [Database migrations](migrations.md)
- [Worker](worker.md)
- [Reverse proxy](reverse-proxy.md)
- [Configuration reference](configuration-reference.md)
- [Releasing](../development/releasing.md)

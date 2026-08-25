# Configuration Reference

Every setting is read from a `FORMS_`-prefixed environment variable, or from a
`.env` file in the working directory. The API, the worker, the operator CLI and
Alembic all read the same variables.

[`.env.example`](https://github.com/hymical/forms/blob/main/.env.example) in the
repository carries the same list with commentary.

## Database

| Variable | Default | Meaning |
| --- | --- | --- |
| `FORMS_DATABASE_URL` | *required* | SQLAlchemy database URL |

No default, on purpose. See [Installation](../getting-started/installation.md).

## Ingestion limits

| Variable | Default | Meaning |
| --- | --- | --- |
| `FORMS_MAX_BODY_BYTES` | `262144` | Largest accepted request body, in bytes |
| `FORMS_MAX_FIELDS` | `100` | Largest number of name/value pairs |
| `FORMS_MAX_FIELD_NAME_LENGTH` | `128` | Largest field name, in characters |
| `FORMS_MAX_FIELD_VALUE_LENGTH` | `16384` | Largest field value, in characters |

A repeated field name counts once per submitted value. See
[Form ingestion](../guides/form-ingestion.md).

## Rate limiting

| Variable | Default | Meaning |
| --- | --- | --- |
| `FORMS_RATE_LIMIT_ENABLED` | `true` | Enforce the public ingestion rate limits |
| `FORMS_RATE_LIMIT_IP_REQUESTS` | `60` | Attempts one source address may make per window |
| `FORMS_RATE_LIMIT_IP_WINDOW_SECONDS` | `60` | How long the per-address window lasts |
| `FORMS_RATE_LIMIT_ENDPOINT_REQUESTS` | `600` | Attempts one endpoint may receive per window |
| `FORMS_RATE_LIMIT_ENDPOINT_WINDOW_SECONDS` | `60` | How long the per-endpoint window lasts |
| `FORMS_RATE_LIMIT_IP_SECRET` | unset | Secret keying the digest addresses are counted under |
| `FORMS_TRUSTED_PROXY_HOPS` | `0` | Reverse proxies of your own in front of this process |

These apply only to `POST /f/{endpoint_id}`. Management routes and `/health` are
not affected. See [Rate limiting](../guides/rate-limiting.md).

!!! warning "`FORMS_TRUSTED_PROXY_HOPS` is security sensitive"

    Leaving it at `0` behind a proxy makes every visitor share one bucket.
    Setting it higher than your real chain lets clients pick their own. See
    [Reverse proxy](reverse-proxy.md).

If you set `FORMS_RATE_LIMIT_IP_SECRET`, every API process must be given the same
value. It must be at least 16 characters.

## Webhook delivery

| Variable | Default | Meaning |
| --- | --- | --- |
| `FORMS_WEBHOOK_CONNECT_TIMEOUT_SECONDS` | `5` | Wait for a webhook to accept a connection |
| `FORMS_WEBHOOK_READ_TIMEOUT_SECONDS` | `10` | Wait for a webhook to respond |
| `FORMS_WEBHOOK_MAX_ATTEMPTS` | `5` | Attempts before a delivery is given up on |
| `FORMS_WEBHOOK_RETRY_INITIAL_SECONDS` | `10` | Wait before the second attempt; later waits double |
| `FORMS_WEBHOOK_RETRY_MAX_SECONDS` | `3600` | Cap on the wait between attempts |
| `FORMS_ALLOW_PRIVATE_WEBHOOK_TARGETS` | `false` | Permit loopback and private webhook targets |

!!! danger "`FORMS_ALLOW_PRIVATE_WEBHOOK_TARGETS` is development only"

    Enabling it in production lets anyone who can create an endpoint reach your
    internal network. See [Security](../architecture/security.md).

## Worker

| Variable | Default | Meaning |
| --- | --- | --- |
| `FORMS_WORKER_POLL_SECONDS` | `1` | How often an idle worker looks for work |
| `FORMS_WORKER_BATCH_SIZE` | `10` | Deliveries a worker claims at once |
| `FORMS_WORKER_LEASE_SECONDS` | `60` | How long a worker's claim holds |

The lease must comfortably outlast the connect and read timeouts combined. See
[Worker](worker.md).

## There is no management key setting

Deliberately. Keys live in the database so that creating and revoking one needs
no restart, and so that a revoked key stops working on the very next request. See
[Authentication](../api/authentication.md).

## Validation

Settings are a typed model, validated at startup. A limit of zero or a negative
window is rejected before the process serves anything, rather than producing a
service that silently accepts nothing or divides by zero later.

## Related

- [Configuration](../getting-started/configuration.md) for what to set first
- [Reverse proxy](reverse-proxy.md)
- [Worker](worker.md)

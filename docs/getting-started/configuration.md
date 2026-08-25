# Configuration

Every setting is read from a `FORMS_`-prefixed environment variable, or from a
`.env` file in the working directory. [`.env.example`](https://github.com/hymical/forms/blob/main/.env.example)
lists all of them with their defaults.

This page covers what you need to get running. The complete table is in the
[Configuration reference](../operations/configuration-reference.md).

## The one required setting

```bash
FORMS_DATABASE_URL=postgresql+psycopg://forms:forms@localhost:5432/forms
```

There is no default. The API, the worker, the operator CLI and Alembic all read
this same variable, so a single `.env` file configures the whole system.

## What you will probably set next

| Variable | Default | Why you might change it |
| --- | --- | --- |
| `FORMS_MAX_BODY_BYTES` | `262144` | Your forms carry more text than a quarter megabyte |
| `FORMS_MAX_FIELDS` | `100` | A form with many checkboxes |
| `FORMS_RATE_LIMIT_IP_REQUESTS` | `60` | The default per-source budget is wrong for your traffic |
| `FORMS_TRUSTED_PROXY_HOPS` | `0` | You run behind a reverse proxy. See below |
| `FORMS_WEBHOOK_MAX_ATTEMPTS` | `5` | You want a longer or shorter retry schedule |
| `FORMS_ALLOW_PRIVATE_WEBHOOK_TARGETS` | `false` | Local development against a webhook on your own machine |

## Two settings worth reading about before you deploy

!!! warning "`FORMS_TRUSTED_PROXY_HOPS` behind a proxy"

    Left at `0` behind a reverse proxy, every visitor is counted as your proxy
    and they all share one rate limit bucket. Set too high, a client can forge
    `X-Forwarded-For` entries and choose its own bucket. It must match your real
    chain exactly. See [Reverse proxy](../operations/reverse-proxy.md).

!!! danger "`FORMS_ALLOW_PRIVATE_WEBHOOK_TARGETS` in production"

    This lifts the check that stops a webhook destination pointing at loopback,
    private or link-local addresses. Enabling it in production lets anyone who
    can create an endpoint reach your internal network. It exists for local
    development only.

## There is no API key setting

There is deliberately no `FORMS_API_KEY` variable. Management keys live in the
database so that creating and revoking one needs no restart, and so that a
revoked key stops working on the very next request rather than on the next
deploy. See [Authentication](../api/authentication.md).

## Next

- [Quick Start](quick-start.md) to accept your first submission
- [Configuration reference](../operations/configuration-reference.md) for every variable

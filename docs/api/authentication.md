# Authentication

Administering the service needs a credential. Submitting a form does not.

| Route | Credential |
| --- | --- |
| `POST /endpoints` | Management API key required |
| `GET /endpoints` | Management API key required |
| `GET /endpoints/{endpoint_id}` | Management API key required |
| `PATCH /endpoints/{endpoint_id}` | Management API key required |
| `GET /deliveries` | Management API key required |
| `GET /deliveries/{delivery_id}` | Management API key required |
| `POST /deliveries/{delivery_id}/replay` | Management API key required |
| `POST /f/{endpoint_id}` | **Public**, no credential |
| `GET /health` | **Public**, no credential |

## What a management API key is

An opaque bearer token that administers the whole service. It is **not** a user
account, not a login, and not scoped to a tenant: there are no users, accounts,
organizations, roles or permissions in this build, and every valid key can do
everything a management key can do.

### Not the same thing as a webhook secret

| Credential | Proves | To |
| --- | --- | --- |
| `whsec_...` webhook signing secret | A delivery came from Hymical Forms | *Your* server |
| `hym_live_...` management API key | A management request came from you | Hymical Forms |

They point in opposite directions and neither can be used in place of the other.

## Creating the first key

Keys are created against the database with the operator CLI, never over HTTP. A
route that issued a management credential without needing one would be an
unauthenticated way to gain full management access, which is the thing this
boundary exists to remove. Nothing is generated at startup, and no key ships with
the repository.

```bash
python -m hymical_forms.cli create-key --name local-admin
```

```
Created management API key mk_634c4efc22fb40fab4b19b82202b23bb (local-admin).

    hym_live_EXAMPLEONLYNOTAREALKEYREPLACETHISWITHYOURS

Save this key now. It is shown here and nowhere else: the server stores
only a digest of it and cannot show it again. If you lose it, create a
replacement and revoke this one by its key ID.
```

!!! danger "That line is the only time the key is ever displayed"

    The server stores a SHA-256 digest of it and nothing else, so there is no
    command, route or database query that can show it to you again. Losing a key
    means creating another one and revoking the old one by its key ID, which
    `list-keys` can still tell you.

The command reads `FORMS_DATABASE_URL`, the same setting everything else reads,
and refuses to run against a database that is not at the migration revision this
build expects.

!!! note "A key is not configuration"

    There is no `FORMS_API_KEY` variable, and there should not be. Keys live in
    the database so they can be created and revoked without restarting anything,
    and so that revoking one takes effect on the very next request rather than on
    the next deploy.

## Authenticating a request

```bash
curl -X POST http://127.0.0.1:8000/endpoints \
  -H 'Authorization: Bearer hym_live_REPLACE_WITH_YOUR_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"id": "contact-form", "name": "Contact form"}'
```

The `Bearer` scheme is required. Credentials are never accepted in a query
parameter, where they would end up in access logs, browser history and `Referer`
headers.

## Refusals

| Status | Code | Cause |
| --- | --- | --- |
| `401` | `authentication_required` | No bearer credential was supplied |
| `401` | `invalid_api_key` | The credential is malformed, unknown or revoked |

Both carry `WWW-Authenticate: Bearer`.

They are deliberately `401` and not `403`. A `403` says the caller is known and
not permitted, which needs a permission model this build does not have.

`invalid_api_key` is one answer for malformed, unknown and revoked credentials
alike, so that a guesser cannot sort their attempts into "nearly right" and
"wrong". The credential a request sent is never echoed back in an error.

## Listing keys

```bash
python -m hymical_forms.cli list-keys
```

```
KEY ID                               NAME         PREFIX             CREATED                    LAST USED  STATUS
mk_634c4efc22fb40fab4b19b82202b23bb  local-admin  hym_live_EXAMPLE1  2026-08-24T22:53:28+00:00  never      active
```

Only what the database holds, which is no credential. `PREFIX` is the first few
characters of the key, kept so that you can tell two credentials apart in this
listing and match one against the key you saved.

There is no HTTP route that lists keys.

## Revoking a key

```bash
python -m hymical_forms.cli revoke-key mk_634c4efc22fb40fab4b19b82202b23bb
```

The key stops authenticating on the next request. Nothing caches a credential, so
revocation is immediate rather than eventual.

The row is not deleted. It keeps the key's identity, when it was created, when it
was last used and when it was withdrawn, which is what makes a revoked key still
explainable later. Revoking twice is accepted and reports the moment the key
actually stopped working.

**Rotation beyond create plus revoke is not implemented.** To rotate: create a new
key, move your callers onto it, then revoke the old one.

## Key format

`hym_live_` followed by 32 random bytes from the operating system, rendered as
unpadded base64url: 43 characters, 256 bits of entropy. The prefix is there so an
operator who finds the string in a config file knows immediately whose credential
it is and what to revoke.

Only a hex SHA-256 digest of the whole key is stored, and lookup is by that
digest, so authentication is one indexed read. The reasoning behind using a plain
digest rather than password-style hashing is in
[Security](../architecture/security.md).

## Related

- [Endpoints](endpoints.md) and [Deliveries](deliveries.md) for the routes this key opens
- [Security](../architecture/security.md) for the design behind the boundary
- [Errors](errors.md) for the full error table

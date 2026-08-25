# Security

What this build protects, how, and where the edges are.

## Two boundaries, on purpose

| Boundary | Routes | Credential |
| --- | --- | --- |
| Public | `POST /f/{endpoint_id}`, `GET /health` | None, by design |
| Management | Everything else | `hym_live_...` bearer key |

Form ingestion **cannot** require a credential: the URL goes in the `action`
attribute of somebody's HTML form, and a browser form has no way to send a
header. So it is protected by [rate limiting](../guides/rate-limiting.md) rather
than by authentication.

Everything that administers the service sits behind one authentication
dependency, declared by every management route, so the rules cannot drift apart
route by route.

## What is never stored

| Secret | What the database holds |
| --- | --- |
| Management API key | A hex SHA-256 digest, and nothing else |
| Client IP address | A SHA-256 digest, optionally HMAC-keyed |
| Webhook response bodies | Nothing. They are never stored |

A management key exists in this process for exactly as long as the request that
carried it. The CLI that mints one prints it once and hands the storage layer
only the digest, so that function could not persist the credential even if a
caller wanted it to.

## Why a plain digest for API keys

Password-style slow hashing is deliberately not used. It exists because passwords
are low-entropy and guessable; a management key is 32 random bytes from the
operating system, 256 bits, and an attacker holding the digest has nothing to
guess at. Slow hashing would add latency to every management request and buy
nothing.

There is no server-side pepper either, for the same reason. A pepper protects a
digest that is feasible to attack offline, which a 256-bit random secret is not,
and it would introduce a second secret whose loss would silently invalidate every
key in the table.

Lookup is by digest, so authentication is one indexed read, and the comparison
that decides the answer uses a constant-time compare.

## Why an optional secret for IP addresses

The opposite reasoning applies, because the input space is tiny.

An unkeyed SHA-256 of an IPv4 address is **not** privacy: there are only about
four billion of them, and anybody holding the table can enumerate the lot. It
keeps addresses out of a casual dump and gives the column a fixed width, and that
is all it claims.

`FORMS_RATE_LIMIT_IP_SECRET` keys the digest with HMAC and makes it genuinely one
way. The usual objection to introducing a second secret does not apply here:
these counters live for one window, so changing or losing the secret costs at most
one window of accounting rather than invalidating anything durable. That is why
it is optional rather than required.

## Error responses disclose nothing

- Driver messages carry table names, SQL text and sometimes connection details,
  so no database exception reaches the client. A storage failure is `503
  storage_unavailable` with a fixed message.
- An unexpected failure is an opaque `500`. An error the service did not
  anticipate is exactly the one whose details should not be published.
- `invalid_api_key` is one answer for malformed, unknown and revoked credentials
  alike, so a guesser cannot sort attempts into "nearly right" and "wrong".
- The credential a request sent is never echoed back.
- An idempotency conflict says the content differs. It never describes the
  earlier submission, so a key cannot be used to read back somebody else's form.
- Submitted field values are never returned by a public route, and never by a
  delivery view. The only routes that return them are the authenticated
  submission detail and export routes, where returning them is the request. See
  [Data handling](../reference/data-handling.md).

## SSRF guardrails

A webhook destination must use `http` or `https` and must not name a loopback,
private, link-local, multicast, reserved or unspecified address. Redirects are
not followed.

!!! warning "This is a guardrail, not a defence"

    Hostnames are **not** resolved. A name that resolves to a private address
    still passes, and DNS rebinding is not addressed at all.

    Closing this properly means resolving at request time and pinning the
    connection to the validated address. Treat the current checks as protection
    against a mistake, not against an attacker who holds a management key.

Authentication narrows who can configure a destination to whoever holds a
management key. It does not make the checks complete.

## Client address trust

`X-Forwarded-For` is ignored by default. It is text the client writes, and
trusting it would hand every client its own private rate limit.

It is read only when `FORMS_TRUSTED_PROXY_HOPS` says how many proxies of your own
to count back from the right. Setting it higher than your real chain is a
vulnerability, not a misconfiguration: it lets a client choose its own bucket.
See [Reverse proxy](../operations/reverse-proxy.md).

## Known gaps

- **No user, account or role model.** Every valid management key can do
  everything a management key can do. Separate keys are useful for revoking one
  caller without disturbing another, and nothing else yet.
- **A rejected submission reveals whether an endpoint ID exists**, which allows
  enumeration. This is unavoidable while form ingestion is public.
- **Idempotency keys are shared across all clients of an endpoint.** Guessing
  another client's key returns that submission's ID and timestamp, though never
  its contents. Random keys of the required length make this impractical.
- **A signing secret cannot be rotated in place.** Rotation happens only as a
  side effect of changing the destination.
- **No spam protection.** Rate limiting bounds volume, not content.

The full list is in [Limitations](../reference/limitations.md).

## Related

- [Authentication](../api/authentication.md) for creating and revoking keys
- [Rate limiting](../guides/rate-limiting.md)
- [Reverse proxy](../operations/reverse-proxy.md)

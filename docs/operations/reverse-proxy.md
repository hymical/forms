# Reverse Proxy

Almost every real deployment puts something in front of the API: nginx, Caddy, a
cloud load balancer, an ingress controller. That changes one thing this service
cares about, and it is security sensitive.

## The problem

Rate limiting counts submissions per client address. By default the client
address is the **socket peer address** the ASGI server reports.

Behind a proxy, the socket peer **is your proxy**. Every visitor in the world
arrives from the same address, they all land in one rate limit bucket, and the
first sixty of them per minute exhaust it for everybody.

## The fix

Tell the service how many proxies of your own stand in front of it:

```bash
FORMS_TRUSTED_PROXY_HOPS=1
```

Then make sure your proxy actually appends the header:

=== "nginx"

    ```nginx
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    ```

    `$proxy_add_x_forwarded_for` appends the peer nginx saw to whatever the
    client sent, which is the behaviour this service expects.

=== "Caddy"

    ```
    forms.example.com {
        reverse_proxy 127.0.0.1:8000
    }
    ```

    Caddy sets `X-Forwarded-For` by appending the client address by default.

## Why it counts from the right

`X-Forwarded-For` is a comma-separated list, and each proxy in the chain appends
the address it saw. So the list reads:

```
X-Forwarded-For: <whatever the client claimed>, <what your outer proxy saw>
```

Everything the client wrote sits on the **left**. The only entry you can trust is
the one your own proxy appended, and that is the **last** one. With two proxies of
your own it is the second from last, and so on.

`FORMS_TRUSTED_PROXY_HOPS` is how many entries to count back from the right.

!!! danger "Set it to your real chain, never higher"

    | Value | Behind one proxy | Behind no proxy |
    | --- | --- | --- |
    | `0` (default) | Every visitor shares your proxy's bucket | Correct |
    | `1` | Correct | A client can forge the whole header and pick its own bucket |
    | `2` | A client can forge an entry and pick its own bucket | Worse |

    A value larger than your actual chain reads an entry the client wrote, which
    means every client gets its own private rate limit. That is the same as
    having no rate limit at all.

If the header is missing, or carries fewer entries than you configured, the
socket peer is used instead rather than the header being half believed.

## Nothing else trusts the header

`X-Forwarded-For` is used for rate limit accounting and nothing else. It does not
affect authentication, routing, logging or what is stored. No raw address is
persisted anywhere: the rate limiter stores a digest. See
[Rate limiting](../guides/rate-limiting.md).

## Body size

The body cap is enforced in this service, in ASGI middleware, before the form
parser runs. A request declaring an oversized `Content-Length` is refused before
a body byte is read.

Your proxy probably has its own limit as well, and it will usually be the one a
client hits first. nginx defaults to `client_max_body_size 1m`, which is above
this service's `FORMS_MAX_BODY_BYTES` default of 256 KiB, so the service's limit
applies. If you raise `FORMS_MAX_BODY_BYTES`, raise the proxy's limit to match or
the proxy will refuse the request with its own error page instead of the JSON
envelope.

## Running several API replicas

Nothing needs sticky sessions. Rate limit counters live in PostgreSQL, so two
replicas enforce one limit between them rather than one each. Round-robin is
fine.

Give every replica the same `FORMS_RATE_LIMIT_IP_SECRET` if you set one, or they
will count the same client under different subjects.

## Related

- [Rate limiting](../guides/rate-limiting.md) for what the address is used for
- [Configuration reference](configuration-reference.md)
- [Security](../architecture/security.md)

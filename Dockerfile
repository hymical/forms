# One image for every Hymical Forms process. The command decides which one runs:
#
#   (default)                          the API, on port 8000
#   python -m hymical_forms.worker     the delivery worker
#   alembic upgrade head               apply migrations, then exit
#   python -m hymical_forms.cli ...    operator commands (keys, retention cleanup)
#
# Configuration comes from FORMS_* environment variables only. Nothing secret is
# baked into the image. See docs/operations/deployment.md.

FROM python:3.13-slim AS build

WORKDIR /src
# Only what the wheel is built from, so a change to the docs or tests does not
# invalidate this layer.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .


FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# A fixed numeric UID, so a Kubernetes runAsNonRoot check can verify it without
# resolving a name.
RUN groupadd --system --gid 10001 forms \
    && useradd --system --uid 10001 --gid forms --no-create-home \
        --home-dir /app --shell /usr/sbin/nologin forms

WORKDIR /app

COPY --from=build /wheels /wheels
RUN pip install /wheels/*.whl && rm -rf /wheels

# Alembic looks for its ini file in the working directory. It names the
# migrations by package, so the ones shipped inside the installed wheel are used.
COPY alembic.ini ./

USER 10001:10001

EXPOSE 8000

# --no-proxy-headers: uvicorn would otherwise rewrite the client address from
# X-Forwarded-For when the peer is 127.0.0.1, and FORMS_TRUSTED_PROXY_HOPS is
# meant to be the only thing that decides whether that header is believed.
CMD ["uvicorn", "hymical_forms.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]

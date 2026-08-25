"""
application settings, read from ``FORMS_``-prefixed environment variables

Settings are added only when the code actually uses them, so this model covers
the ingestion boundary's protective limits, the traffic limits that guard public
ingestion, and what the delivery worker needs, and nothing speculative.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from hymical_forms.ratelimit import RateLimit
from hymical_forms.webhooks import RetryPolicy


class Settings(BaseSettings):
    """
    runtime configuration for a hymical forms process
    """

    model_config = SettingsConfigDict(
        env_prefix="FORMS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    database_url: str = Field(
        description=(
            "SQLAlchemy database URL. PostgreSQL is the intended production database, "
            "for example postgresql+psycopg://user:password@localhost:5432/forms."
        ),
    )
    max_body_bytes: int = Field(
        default=256 * 1024,
        ge=1,
        description="Largest request body accepted, in bytes. File uploads are not supported.",
    )
    max_fields: int = Field(
        default=100,
        ge=1,
        description="Largest number of name/value pairs accepted in one submission.",
    )
    max_field_name_length: int = Field(
        default=128,
        ge=1,
        description="Largest field name accepted, in characters.",
    )
    max_field_value_length: int = Field(
        default=16 * 1024,
        ge=1,
        description="Largest field value accepted, in characters.",
    )
    webhook_connect_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        description="How long to wait for a webhook destination to accept a connection.",
    )
    webhook_read_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        description="How long to wait for a webhook destination to respond.",
    )
    webhook_max_attempts: int = Field(
        default=5,
        ge=1,
        description="How many delivery attempts a submission gets before it is given up on.",
    )
    webhook_retry_initial_seconds: float = Field(
        default=10.0,
        gt=0,
        description="Wait before the second attempt. Each later wait doubles it.",
    )
    webhook_retry_max_seconds: float = Field(
        default=3600.0,
        gt=0,
        description="Cap on the wait between attempts, however far the backoff has doubled.",
    )
    worker_poll_seconds: float = Field(
        default=1.0,
        gt=0,
        description="How long the worker waits before looking for due deliveries again.",
    )
    worker_batch_size: int = Field(
        default=10,
        ge=1,
        description="How many deliveries a worker claims at once.",
    )
    worker_lease_seconds: float = Field(
        default=60.0,
        gt=0,
        description=(
            "How long a worker's claim on a delivery holds. After this the delivery "
            "becomes claimable again, which is how work is recovered from a worker "
            "that died holding it."
        ),
    )
    allow_private_webhook_targets: bool = Field(
        default=False,
        description=(
            "Permit webhook destinations on loopback and private addresses. "
            "For local development and tests only; enabling it in production "
            "exposes the server to SSRF."
        ),
    )

    rate_limit_enabled: bool = Field(
        default=True,
        description=(
            "Enforce the public ingestion rate limits. On by default, because a "
            "public route with no limit is the exposure this exists to close. "
            "Turn it off only for local development or a test that is about "
            "something else."
        ),
    )
    rate_limit_ip_requests: int = Field(
        default=60,
        ge=1,
        description="Public submission attempts one source address may make per window.",
    )
    rate_limit_ip_window_seconds: int = Field(
        default=60,
        ge=1,
        description="How long the per-address window lasts, in seconds.",
    )
    rate_limit_endpoint_requests: int = Field(
        default=600,
        ge=1,
        description="Public submission attempts one endpoint may receive per window.",
    )
    rate_limit_endpoint_window_seconds: int = Field(
        default=60,
        ge=1,
        description="How long the per-endpoint window lasts, in seconds.",
    )
    rate_limit_ip_secret: str | None = Field(
        default=None,
        min_length=16,
        description=(
            "Secret keying the digest that client addresses are counted under. "
            "Optional: without it the digest is unkeyed, which keeps addresses out "
            "of the table but is not privacy against anyone who can read it. Every "
            "API process must be given the same value."
        ),
    )
    trusted_proxy_hops: int = Field(
        default=0,
        ge=0,
        description=(
            "How many reverse proxies of your own stand in front of this process. "
            "0, the default, means the client address is the socket peer and "
            "X-Forwarded-For is ignored. Set it to the real number of hops, never "
            "higher, or clients can choose their own rate limit bucket."
        ),
    )

    def retry_policy(self) -> RetryPolicy:
        """
        gather the retry settings into the value the delivery code works with
        :returns: the configured retry policy
        """
        return RetryPolicy(
            max_attempts=self.webhook_max_attempts,
            initial_seconds=self.webhook_retry_initial_seconds,
            max_seconds=self.webhook_retry_max_seconds,
        )

    def ip_rate_limit(self) -> RateLimit:
        """
        gather the per-address limit into the value the limiter works with
        :returns: the configured per-address rate limit
        """
        return RateLimit(
            requests=self.rate_limit_ip_requests,
            window_seconds=self.rate_limit_ip_window_seconds,
        )

    def endpoint_rate_limit(self) -> RateLimit:
        """
        gather the per-endpoint limit into the value the limiter works with
        :returns: the configured per-endpoint rate limit
        """
        return RateLimit(
            requests=self.rate_limit_endpoint_requests,
            window_seconds=self.rate_limit_endpoint_window_seconds,
        )

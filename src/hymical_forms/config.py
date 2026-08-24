"""
application settings, read from ``FORMS_``-prefixed environment variables

Settings are added only when the code actually uses them, so this model is
currently limited to the ingestion boundary's protective limits.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

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

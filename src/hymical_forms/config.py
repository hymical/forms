"""
application settings, read from ``FORMS_``-prefixed environment variables

Settings are added only when the code actually uses them, so this model is
currently limited to the ingestion boundary's protective limits.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

"""Shared test fixtures.

Tests build their own application instances so that limits can be lowered to
values that are cheap to exercise, and so that a developer's local environment
can never change a test's outcome.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from pydantic_settings import SettingsConfigDict

from hymical_forms.app import create_app
from hymical_forms.config import Settings

URLENCODED_HEADERS = {"content-type": "application/x-www-form-urlencoded"}

ClientFactory = Callable[..., TestClient]


class IsolatedSettings(Settings):
    """Settings that ignore a local ``.env``, so a developer's file cannot skew a run."""

    model_config = SettingsConfigDict(env_file=None)


@pytest.fixture(autouse=True)
def _ignore_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hide any ``FORMS_*`` variables the developer happens to have exported."""
    for name in list(os.environ):
        if name.startswith("FORMS_"):
            monkeypatch.delenv(name)


def build_settings(**overrides: int) -> Settings:
    """Build settings from defaults and explicit overrides only."""
    return IsolatedSettings(**overrides)


@pytest.fixture
def make_client() -> Iterator[ClientFactory]:
    """Return a factory for clients bound to an app with the given setting overrides."""
    with ExitStack() as stack:

        def factory(**overrides: int) -> TestClient:
            return stack.enter_context(TestClient(create_app(build_settings(**overrides))))

        yield factory


@pytest.fixture
def client(make_client: ClientFactory) -> TestClient:
    """A client for an application running on default settings."""
    return make_client()

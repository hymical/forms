"""
shared test fixtures

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
    """
    settings that ignore a local ``.env``, so a developer's file cannot skew a run
    """

    model_config = SettingsConfigDict(env_file=None)


@pytest.fixture(autouse=True)
def _ignore_ambient_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    hide any ``FORMS_*`` variables the developer happens to have exported
    :param monkeypatch: pytest fixture used to remove the variables for one test
    """
    for name in list(os.environ):
        if name.startswith("FORMS_"):
            monkeypatch.delenv(name)


def build_settings(**overrides: int) -> Settings:
    """
    build settings from defaults and explicit overrides only
    :param overrides: setting values to replace the built-in defaults
    :returns: settings that ignore the ambient environment
    """
    return IsolatedSettings(**overrides)


@pytest.fixture
def make_client() -> Iterator[ClientFactory]:
    """
    provide a factory for clients bound to an app with the given setting overrides
    :returns: a factory that accepts setting overrides and returns a test client
    """
    with ExitStack() as stack:

        def factory(**overrides: int) -> TestClient:
            """
            build a client for an app configured with the given overrides
            :param overrides: setting values to replace the built-in defaults
            :returns: a test client closed when the fixture tears down
            """
            return stack.enter_context(TestClient(create_app(build_settings(**overrides))))

        yield factory


@pytest.fixture
def client(make_client: ClientFactory) -> TestClient:
    """
    provide a client for an application running on default settings
    :param make_client: factory for clients bound to a configured app
    :returns: a test client for an app on default settings
    """
    return make_client()

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apps.insurance_claims.api import create_app
from apps.insurance_claims.config import FixedClock
from tests.fakes import TODAY, ScriptedAdapter, make_settings
from tests.scenarios import Conversation


@pytest.fixture
def make_client():
    """Build a TestClient around a fresh app; defaults to a ScriptedAdapter and a fixed clock."""
    clients: list[TestClient] = []

    def _make(*, adapter=None, settings=None, store=None, mailer=None, raise_server_exceptions=True):
        app = create_app(
            settings or make_settings(),
            adapter=adapter or ScriptedAdapter(),
            clock=FixedClock(TODAY),
            store=store,
            mailer=mailer,
        )
        client = TestClient(app, raise_server_exceptions=raise_server_exceptions)
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.close()


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def talk(make_client):
    """Start a scripted conversation: ``talk(session={...}, mailer=..., **settings)``."""
    return lambda **options: Conversation(make_client, **options)

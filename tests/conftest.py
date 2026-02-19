"""Shared test fixtures for directdnsonly test suite."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from directdnsonly.app.db import Base
from directdnsonly.app.db.models import (
    Domain,
    Key,
)  # noqa: F401 — registers models with Base


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine):
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def patch_connect(db_session, monkeypatch):
    """Patch connect() at every call-site, returning the shared test session.

    Modules that import connect() directly (e.g. utils, reconciler) are
    patched at their local name so the in-memory SQLite session is used
    instead of trying to read from vyper config.
    """
    _factory = lambda: db_session  # noqa: E731
    monkeypatch.setattr("directdnsonly.app.utils.connect", _factory)
    monkeypatch.setattr("directdnsonly.app.reconciler.connect", _factory)
    monkeypatch.setattr("directdnsonly.app.peer_sync.connect", _factory)
    return db_session

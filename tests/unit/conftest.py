"""Shared pytest fixtures for orchestrator-engine test suites.

Provides an in-memory SQLite session with all models loaded, and
guarantees the encryption key is set before any EncryptedString column
is touched (Employee.demographics_encrypted, etc.).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


@pytest.fixture(autouse=True)
def _ensure_encryption_key():
    """Ensure an encryption key is available before any model touches
    EncryptedString columns."""
    os.environ.setdefault("ENCRYPTION_KEY", "test-key-for-orchestrator-tests")
    from app.config import settings as _settings
    if not _settings.encryption_key:
        _settings.encryption_key = "test-key-for-orchestrator-tests"
    yield


@pytest.fixture
def db_session():
    """In-memory SQLite with all models loaded for a single test."""
    import app.database as db_module
    from app.database import Base
    # Register every model so metadata.create_all builds the full schema.
    from app.models import (  # noqa: F401
        employer, employee, service, provider, claim,
        clinical_guideline, clinical_determination, price_data,
        price_comparison, care_episode, audit_log, benchmark_query,
    )

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = SessionLocal()

    original_session_local = db_module.SessionLocal
    db_module.SessionLocal = SessionLocal
    try:
        yield session
    finally:
        session.close()
        db_module.SessionLocal = original_session_local
        Base.metadata.drop_all(bind=engine)
        engine.dispose()

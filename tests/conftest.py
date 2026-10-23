"""Test setup. The environment is set BEFORE the app is imported, because the app reads its settings at import time.
Every test gets an empty database, so no test can depend on another."""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"
os.environ["ADMIN_SETUP_SECRET"] = "test-secret"
os.environ["FLUTTERWAVE_WEBHOOK_HASH"] = "test-hash"
os.environ["FLUTTERWAVE_SECRET_KEY"] = "fw-test-key"
os.environ["ANTHROPIC_API_KEY"] = ""          # no AI in tests: nothing may spend money

import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app.main import app
from app.models import Base


@pytest.fixture(autouse=True)
def fresh_database():
    Base.metadata.drop_all(dbm.engine)
    Base.metadata.create_all(dbm.engine)
    yield


@pytest.fixture
def db():
    s = dbm.SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def no_rate_limit_leakage():
    """The in-memory limiters live for the whole process; reset them so tests stay independent."""
    from app import main
    for lim in (main._track_limiter, main._promo_limiter, main._demo_limiter):
        lim._hits.clear()
    yield

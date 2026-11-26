import itertools
import os
import tempfile

import pytest

# Must run before anything from `app` is imported: app/db.py reads DATABASE_URL at import time.
_tmp = tempfile.mkdtemp(prefix="wallet-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["JWT_SECRET"] = "test-secret-test-secret-test-secret-123456"
os.environ["ADMIN_KEY"] = "test-admin-key"
os.environ["SIGNUP_BONUS_CENTS"] = "100000"
os.environ.pop("ENABLE_CHAOS", None)

from fastapi.testclient import TestClient  # noqa: E402
from app.database import DATABASE_URL  # noqa: E402
from app.main import app, chaos_state  # noqa: E402

# Guard: refuse to run if the app is not on the throwaway database.
if "wallet-test-" not in DATABASE_URL:
    raise RuntimeError(f"Tests are pointed at a real database: {DATABASE_URL.split('@')[-1]}")

ADMIN = {"X-Admin-Key": "test-admin-key"}
_counter = itertools.count(1)


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def reset_chaos():
    chaos_state.update({"latency_ms": 0, "error_rate": 0.0})
    yield
    chaos_state.update({"latency_ms": 0, "error_rate": 0.0})


@pytest.fixture()
def make_user(client):
    """Returns a function that registers a fresh user and gives back (username, auth header)."""

    def _make(password: str = "correct-horse-battery"):
        name = f"user_{next(_counter)}"
        assert client.post("/auth/register", json={"username": name, "password": password}).status_code == 201
        token = client.post("/auth/login", json={"username": name, "password": password}).json()["access_token"]
        return name, {"Authorization": f"Bearer {token}"}

    return _make

import os

# Point everything at the test database before the app is imported.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://localhost/airhoops_test")

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

from app import models  # noqa: F401  (registers tables)
from app.config import Settings
from app.db import set_engine
from app.main import create_app
from app.rooms import RoomManager


class FakeClock:
    """Lets tests fast-forward reconnect grace periods and heartbeat timeouts."""

    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(os.environ["DATABASE_URL"])
    SQLModel.metadata.drop_all(eng)
    SQLModel.metadata.create_all(eng)
    set_engine(eng)
    yield eng
    SQLModel.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture(autouse=True)
def clean_db(engine):
    yield
    with engine.begin() as conn:
        conn.exec_driver_sql("TRUNCATE throws, scores, matches, users CASCADE")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings() -> Settings:
    return Settings(sweep_interval_s=0, reconnect_grace_s=30, heartbeat_timeout_s=25, max_players=4)


@pytest.fixture
def manager(settings, clock) -> RoomManager:
    return RoomManager(settings, clock)


@pytest.fixture
def client(settings, manager):
    # Entering the client gives every WebSocket session one shared event loop.
    with TestClient(create_app(settings, manager)) as c:
        yield c


def make_guest(client: TestClient, nickname: str) -> tuple[str, str]:
    res = client.post("/auth/guest", json={"nickname": nickname})
    assert res.status_code == 201, res.text
    body = res.json()
    return body["token"], body["user"]["id"]


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def receive_until(ws, kind: str, limit: int = 10, **match):
    """Read messages until one of `kind` (and matching fields) arrives."""
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == kind and all(msg.get(k) == v for k, v in match.items()):
            return msg
    raise AssertionError(f"no {kind} {match} within {limit} messages")

"""Integration tests for GET /api/session/latest.

The endpoint returns the current user's most-recently-touched session so the
frontend can restore the last chart on load and after login. We drive the real
FastAPI app over httpx/ASGI with an in-memory SQLite DB and a stubbed current
user, and register the matching Session objects in the in-memory store so
hydrate_session resolves them without re-fetching klines (no network).
"""
import datetime as dt

import pandas as pd
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import main
from backend.auth import current_user
from backend.db import Base, get_db
from backend.models import ReplaySnapshot, User
from backend.replay import Session, Trade

TEST_USER_ID = 1


def _session_obj(sid, symbol, trades=None):
    df = pd.DataFrame(
        [{"time": 1_000, "open": 100, "high": 110, "low": 90, "close": 105, "volume": 1.0}]
    )
    s = Session(id=sid, symbol=symbol, market="spot", tf="4h",
                start="s", end="e", df=df, cursor=0, user_id=TEST_USER_ID)
    s.trades = trades or []
    return s


@pytest_asyncio.fixture
async def client():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _get_db():
        async with factory() as s:
            yield s

    user = User(id=TEST_USER_ID, email="u@example.com")
    main.app.dependency_overrides[get_db] = _get_db
    main.app.dependency_overrides[current_user] = lambda: user
    main.store._sessions.clear()
    transport = ASGITransport(app=main.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.factory = factory          # so tests can insert snapshot rows
        yield c
    main.app.dependency_overrides.clear()
    main.store._sessions.clear()
    await engine.dispose()


@pytest.mark.asyncio
async def test_latest_returns_most_recently_updated_with_trades(client):
    async with client.factory() as db:
        db.add_all([
            ReplaySnapshot(sid="old", user_id=TEST_USER_ID, symbol="SOLUSDT",
                           market="spot", tf="4h", snapshot={},
                           updated_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)),
            ReplaySnapshot(sid="new", user_id=TEST_USER_ID, symbol="BTCUSDT",
                           market="spot", tf="1h", snapshot={},
                           updated_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)),
        ])
        await db.commit()
    # register both in the store so hydrate_session resolves them in-memory
    main.store._sessions["old"] = _session_obj("old", "SOLUSDT")
    main.store._sessions["new"] = _session_obj("new", "BTCUSDT", [
        Trade(id="t1", side="long", qty=2.0, order_type="market",
              created_time=0, status="open", entry_time=0, entry_price=100.0),
    ])

    r = await client.get("/api/session/latest")

    assert r.status_code == 200
    data = r.json()
    assert data["id"] == "new"            # newest updated_at wins
    assert data["symbol"] == "BTCUSDT"
    assert len(data["trades"]) == 1
    assert data["trades"][0]["id"] == "t1"


@pytest.mark.asyncio
async def test_latest_204_when_user_has_no_sessions(client):
    r = await client.get("/api/session/latest")
    assert r.status_code == 204


@pytest.mark.asyncio
async def test_client_state_persists_and_restores_via_latest(client):
    # register a session for the user, then POST client_state and read it back
    async with client.factory() as db:
        db.add(ReplaySnapshot(sid="s1", user_id=TEST_USER_ID, symbol="SOLUSDT",
                              market="spot", tf="4h", snapshot={},
                              updated_at=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)))
        await db.commit()
    main.store._sessions["s1"] = _session_obj("s1", "SOLUSDT")

    cs = {
        "indicators": [{"kind": "ema", "period": 200}, {"kind": "cvd"}],
        "hlines": [{"price": 80.5, "color": "#90caf9", "width": 2, "style": 2}],
        "measure": {"s": {"time": 1000, "price": 80}, "e": {"time": 5000, "price": 82}},
        "trade": {"desktop": {"qty": "4", "slOn": True, "slPct": "1.2", "tpOn": False, "tpPct": "1"}},
    }
    r = await client.post("/api/session/s1/client_state", json=cs)
    assert r.status_code == 200 and r.json() == {"ok": True}

    # in-memory session now carries it
    assert main.store._sessions["s1"].client_state == cs
    # and GET latest serializes it back
    latest = await client.get("/api/session/latest")
    assert latest.status_code == 200
    assert latest.json()["client_state"] == cs


@pytest.mark.asyncio
async def test_latest_ignores_other_users_sessions(client):
    async with client.factory() as db:
        db.add(ReplaySnapshot(sid="theirs", user_id=999, symbol="ETHUSDT",
                              market="spot", tf="4h", snapshot={},
                              updated_at=dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)))
        await db.commit()
    main.store._sessions["theirs"] = _session_obj("theirs", "ETHUSDT")

    r = await client.get("/api/session/latest")
    assert r.status_code == 204         # belongs to user 999, not ours

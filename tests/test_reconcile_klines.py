"""Integration tests for POST /api/session/{sid}/reconcile_klines.

Live candles are built client-side from the WS feed and can drift (incomplete
high/low/close) when the tab is suspended. This endpoint overwrites the stored
df rows with authoritative OHLC and appends any contiguous newer closed bars.

We drive the real FastAPI app over httpx/ASGI with an in-memory SQLite DB and a
stubbed current user, registering the Session in the in-memory store so
hydrate_session resolves it without re-fetching klines (no network).
"""
import pandas as pd
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import main
from backend.auth import current_user
from backend.db import Base, get_db
from backend.models import User
from backend.replay import Session

TEST_USER_ID = 1


def _live_session(sid, df):
    s = Session(id=sid, symbol="SOLUSDT", market="spot", tf="4h",
                start="s", end="e", df=df, cursor=len(df) - 1,
                user_id=TEST_USER_ID, is_live=True)
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
        yield c
    main.app.dependency_overrides.clear()
    main.store._sessions.clear()
    await engine.dispose()


def _df(rows):
    return pd.DataFrame(
        [{"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v}
         for (t, o, h, l, c, v) in rows]
    )


@pytest.mark.asyncio
async def test_reconcile_overwrites_drifted_rows(client):
    # bar @2000 drifted: its high/low/close are clipped vs Binance.
    df = _df([(1000, 10, 12, 9, 11, 100),
              (2000, 11, 11.5, 10.5, 11.0, 50)])    # drifted (narrow range)
    main.store._sessions["s1"] = _live_session("s1", df)

    r = await client.post("/api/session/s1/reconcile_klines", json={"klines": [
        {"time": 1000, "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100},
        {"time": 2000, "open": 11, "high": 13, "low": 9.5, "close": 12.5, "volume": 80},
    ]})
    assert r.status_code == 200
    body = r.json()
    assert body == {"updated": 1, "appended": 0}

    row = main.store._sessions["s1"].df.iloc[1]
    assert (row.high, row.low, row.close, row.volume) == (13, 9.5, 12.5, 80)
    # the untouched first row stays put
    assert tuple(main.store._sessions["s1"].df.iloc[0][["high", "low", "close"]]) == (12, 9, 11)


@pytest.mark.asyncio
async def test_reconcile_appends_newer_closed_bar(client):
    df = _df([(1000, 10, 12, 9, 11, 100)])
    main.store._sessions["s2"] = _live_session("s2", df)

    r = await client.post("/api/session/s2/reconcile_klines", json={"klines": [
        {"time": 1000, "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100},
        {"time": 2000, "open": 11, "high": 14, "low": 10, "close": 13, "volume": 70},
    ]})
    assert r.json() == {"updated": 0, "appended": 1}
    s = main.store._sessions["s2"]
    assert len(s.df) == 2
    assert int(s.df.iloc[-1].time) == 2000
    assert s.cursor == 1


@pytest.mark.asyncio
async def test_reconcile_writes_by_position_not_label(client):
    # A df whose index is NOT a clean 0..n RangeIndex (as a slice/extend-history
    # path can leave it). The endpoint must address rows positionally, not by
    # label, or it would write to the wrong/missing row.
    df = _df([(1000, 10, 12, 9, 11, 100),
              (2000, 11, 11.5, 10.5, 11.0, 50),
              (3000, 11, 11.2, 10.8, 11.0, 40)])
    df.index = [5, 6, 7]                                   # non-RangeIndex labels
    main.store._sessions["s3"] = _live_session("s3", df)

    r = await client.post("/api/session/s3/reconcile_klines", json={"klines": [
        {"time": 2000, "open": 11, "high": 13, "low": 9.5, "close": 12.5, "volume": 80},
    ]})
    assert r.json() == {"updated": 1, "appended": 0}
    s = main.store._sessions["s3"]
    # the @2000 bar (positional index 1) is corrected; neighbors untouched
    assert tuple(s.df.iloc[1][["high", "low", "close"]]) == (13, 9.5, 12.5)
    assert tuple(s.df.iloc[0][["high", "low", "close"]]) == (12, 9, 11)
    assert tuple(s.df.iloc[2][["high", "low", "close"]]) == (11.2, 10.8, 11.0)


@pytest.mark.asyncio
async def test_reconcile_noop_when_already_matching(client):
    df = _df([(1000, 10, 12, 9, 11, 100)])
    main.store._sessions["s4"] = _live_session("s4", df)
    r = await client.post("/api/session/s4/reconcile_klines", json={"klines": [
        {"time": 1000, "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100},
    ]})
    assert r.json() == {"updated": 0, "appended": 0}


@pytest.mark.asyncio
async def test_reconcile_rejected_for_replay_session(client):
    df = _df([(1000, 10, 12, 9, 11, 100)])
    s = _live_session("s5", df)
    s.is_live = False                                     # replay session
    main.store._sessions["s5"] = s
    r = await client.post("/api/session/s5/reconcile_klines", json={"klines": [
        {"time": 1000, "open": 10, "high": 99, "low": 1, "close": 50, "volume": 100},
    ]})
    assert r.json() == {"updated": 0, "appended": 0}
    # df untouched
    assert tuple(main.store._sessions["s5"].df.iloc[0][["high", "low", "close"]]) == (12, 9, 11)

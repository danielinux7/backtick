"""Integration tests for the stable per-mode session model.

One session per (user, market, mode); symbol + tf are views within it, trades
are tagged per-symbol. We drive the real FastAPI app over httpx/ASGI with an
in-memory SQLite DB and a stubbed user, and stub fetch_klines so no network is
hit (the riskiest logic — reuse / set_view / reset — runs for real).
"""
import pandas as pd
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import main, replay
from backend.auth import current_user
from backend.db import Base, get_db
from backend.models import User

TEST_USER_ID = 1
TF_SEC = {"15m": 900, "1h": 3600, "4h": 14400}
BASE = 1_700_000_000


def _fake_klines(symbol, tf, start, end, market="spot"):
    """Deterministic synthetic klines: 300 bars at the tf's spacing, aligned to
    the tf grid so cross-tf cursor remaps land on real bars."""
    step = TF_SEC[tf]
    base = BASE - (BASE % step)
    rows = [{"time": base + i * step, "open": 100 + i * 0.1, "high": 100 + i * 0.1 + 1,
             "low": 100 + i * 0.1 - 1, "close": 100 + i * 0.1 + 0.5, "volume": 1.0}
            for i in range(300)]
    return pd.DataFrame(rows)


def _replay_ts(tf, n=50):
    step = TF_SEC[tf]
    return (BASE - (BASE % step)) + n * step


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setattr(replay, "fetch_klines", _fake_klines)
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


def _replay_body(symbol="SOLUSDT", tf="4h", **extra):
    return {"symbol": symbol, "market": "spot", "tf": tf,
            "start": "2023-11-01", "end": "2024-01-01",
            "replay_ts": _replay_ts(tf), "warmup": 100, **extra}


async def _place_market_long(client, sid, qty=2.0):
    r = await client.post(f"/api/session/{sid}/trade",
                          json={"side": "long", "qty": qty, "order_type": "market"})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_mode_has_one_session_and_resumes(client):
    r = await client.post("/api/session", json=_replay_body())
    assert r.status_code == 200, r.text
    replay_sid = r.json()["id"]
    assert r.json()["created"] is True
    await _place_market_long(client, replay_sid)

    # flipping to live is a DIFFERENT session
    live = await client.post("/api/session", json={"symbol": "SOLUSDT", "market": "spot",
                                                   "tf": "4h", "live": True, "warmup": 100})
    assert live.status_code == 200, live.text
    assert live.json()["id"] != replay_sid
    assert live.json()["is_live"] is True

    # back to replay → same session, trade still there, not re-created
    again = await client.post("/api/session", json=_replay_body())
    assert again.json()["id"] == replay_sid
    assert again.json()["created"] is False
    assert len(again.json()["trades"]) == 1


@pytest.mark.asyncio
async def test_symbol_is_a_view_with_per_symbol_trades(client):
    r = await client.post("/api/session", json=_replay_body("SOLUSDT"))
    sid = r.json()["id"]
    await _place_market_long(client, sid, qty=2.0)

    # switch the view to BTC — same session, BTC has no trades yet
    btc = await client.post("/api/session", json=_replay_body("BTCUSDT"))
    assert btc.json()["id"] == sid
    assert btc.json()["created"] is False
    assert btc.json()["symbol"] == "BTCUSDT"
    assert btc.json()["trades"] == []
    await _place_market_long(client, sid, qty=3.0)

    # back to SOL → only the SOL trade shows
    sol = await client.post("/api/session", json=_replay_body("SOLUSDT"))
    assert sol.json()["id"] == sid
    assert len(sol.json()["trades"]) == 1
    assert sol.json()["trades"][0]["qty"] == 2.0
    assert sol.json()["trades"][0]["symbol"] == "SOLUSDT"

    # both trades persist on the session object
    assert len(main.store._sessions[sid].trades) == 2


@pytest.mark.asyncio
async def test_changing_tf_keeps_trades_and_remaps_cursor(client):
    r = await client.post("/api/session", json=_replay_body("SOLUSDT", tf="4h"))
    sid = r.json()["id"]
    t_before = r.json()["current_time"]
    await _place_market_long(client, sid)

    one_h = await client.post("/api/session", json=_replay_body("SOLUSDT", tf="1h"))
    assert one_h.json()["id"] == sid
    assert one_h.json()["tf"] == "1h"
    assert one_h.json()["current_time"] == t_before    # same point in time at the new tf
    assert len(one_h.json()["trades"]) == 1


@pytest.mark.asyncio
async def test_reset_clears_only_current_symbol(client):
    r = await client.post("/api/session", json=_replay_body("SOLUSDT"))
    sid = r.json()["id"]
    await _place_market_long(client, sid)
    btc = await client.post("/api/session", json=_replay_body("BTCUSDT"))
    await _place_market_long(client, sid)
    assert len(main.store._sessions[sid].trades) == 2

    # reset BTC (the current view) → BTC trades cleared, SOL untouched
    reset = await client.post("/api/session", json=_replay_body("BTCUSDT", reset=True))
    assert reset.json()["id"] == sid
    assert reset.json()["trades"] == []
    sol = await client.post("/api/session", json=_replay_body("SOLUSDT"))
    assert len(sol.json()["trades"]) == 1


def test_process_candle_only_touches_active_symbol():
    """A trade tagged for another symbol must not fill/trigger on this symbol's
    candles."""
    df = pd.DataFrame([
        {"time": 1000, "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1.0},
        {"time": 2000, "open": 100, "high": 100, "low": 80, "close": 85, "volume": 1.0},
    ])
    s = replay.Session(id="t", symbol="AAA", market="spot", tf="1h",
                       start="s", end="e", df=df, cursor=0)
    mine = replay.Trade(id="m", symbol="AAA", side="long", qty=1.0, order_type="market",
                        created_time=0, status="open", entry_time=1000, entry_price=100.0, sl=90.0)
    other = replay.Trade(id="o", symbol="BBB", side="long", qty=1.0, order_type="market",
                         created_time=0, status="open", entry_time=1000, entry_price=100.0, sl=90.0)
    s.trades = [mine, other]
    s.cursor = 1
    s.process_candle()
    assert mine.status == "closed" and mine.exit_reason == "sl"
    assert other.status == "open"      # different symbol — left alone

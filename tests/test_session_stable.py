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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend import main, replay, snapshots
from backend.auth import current_user
from backend.db import Base, get_db
from backend.models import ReplaySnapshot, User
from backend.snapshots import hydrate_session

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
        c.factory = factory          # so tests can inspect persisted rows
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
async def test_live_has_one_stable_session_and_resumes(client):
    """Live keeps a single persisted session per (user, market): a second live
    load for the same key resumes it rather than re-creating."""
    live1 = await client.post("/api/session", json={"symbol": "SOLUSDT", "market": "spot",
                                                    "tf": "4h", "live": True, "warmup": 100})
    assert live1.status_code == 200, live1.text
    sid = live1.json()["id"]
    assert live1.json()["is_live"] is True and live1.json()["created"] is True

    live2 = await client.post("/api/session", json={"symbol": "SOLUSDT", "market": "spot",
                                                    "tf": "4h", "live": True, "warmup": 100})
    assert live2.json()["id"] == sid           # same stable session
    assert live2.json()["created"] is False


@pytest.mark.asyncio
async def test_replay_is_fresh_each_load(client):
    """Replay is ephemeral — nothing is persisted to resume, so every load (a new
    sitting, a symbol/tf flip, a date jump) is a brand-new session that starts
    clean. Trades only live in memory for the duration of the one sitting."""
    r1 = await client.post("/api/session", json=_replay_body("SOLUSDT", tf="4h"))
    assert r1.status_code == 200, r1.text
    sid1 = r1.json()["id"]
    assert r1.json()["created"] is True
    await _place_market_long(client, sid1)
    assert len(main.store._sessions[sid1].trades) == 1   # held in-memory this sitting

    # a fresh replay load — different symbol/tf, doesn't matter — is a new session
    r2 = await client.post("/api/session", json=_replay_body("BTCUSDT", tf="1h"))
    assert r2.json()["id"] != sid1
    assert r2.json()["created"] is True
    assert r2.json()["symbol"] == "BTCUSDT" and r2.json()["tf"] == "1h"
    assert r2.json()["trades"] == []


@pytest.mark.asyncio
async def test_replay_writes_no_snapshot_row(client):
    """Placing a replay trade must not persist anything to the DB."""
    r = await client.post("/api/session", json=_replay_body("SOLUSDT"))
    sid = r.json()["id"]
    await _place_market_long(client, sid)

    async with client.factory() as db:
        rows = (await db.execute(select(ReplaySnapshot))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_replay_is_not_restorable_via_latest(client):
    """A replay session left warm is never resurrected by GET /latest (live-only)."""
    r = await client.post("/api/session", json=_replay_body("SOLUSDT"))
    await _place_market_long(client, r.json()["id"])

    latest = await client.get("/api/session/latest")
    assert latest.status_code == 204


@pytest.mark.asyncio
async def test_cold_hydrate_handles_stale_cursor(monkeypatch):
    """A persisted cursor can exceed a freshly-fetched df (extend_history bumps
    it without widening start). Cold hydrate must not index out of bounds: it
    remaps by cursor_time when present, else clamps."""
    monkeypatch.setattr(replay, "fetch_klines", _fake_klines)
    monkeypatch.setattr(snapshots, "fetch_klines", _fake_klines)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = replay.SessionStore()

    base = BASE - (BASE % TF_SEC["4h"])
    async with factory() as db:
        # legacy snapshot (no cursor_time) with an out-of-range cursor
        db.add(ReplaySnapshot(sid="legacy", user_id=1, symbol="SOLUSDT", market="spot",
                              tf="4h", is_live=False,
                              snapshot={"symbol": "SOLUSDT", "market": "spot", "tf": "4h",
                                        "start": "2024-01-01", "end": "2024-06-01",
                                        "cursor": 99999, "trades": []}))
        # newer snapshot carrying cursor_time → remaps to that bar
        db.add(ReplaySnapshot(sid="timed", user_id=1, symbol="SOLUSDT", market="spot",
                              tf="4h", is_live=False,
                              snapshot={"symbol": "SOLUSDT", "market": "spot", "tf": "4h",
                                        "start": "2024-01-01", "end": "2024-06-01",
                                        "cursor": 99999, "cursor_time": base + 60 * TF_SEC["4h"],
                                        "trades": []}))
        await db.commit()

        legacy = await hydrate_session(db, store, "legacy", 1)
        assert legacy is not None
        assert 0 <= legacy.cursor < len(legacy.df)        # clamped, no IndexError
        legacy.current_price()                             # must not raise

        timed = await hydrate_session(db, store, "timed", 1)
        assert timed.current_time() == base + 60 * TF_SEC["4h"]   # remapped by time
    await engine.dispose()


def test_replay_fill_time_refined_to_the_minute(monkeypatch):
    """A limit that fills inside a 4h candle is stamped at the minute price
    actually crossed it (from 1m klines), not the 4h candle open — so it maps
    to the right bar on lower timeframes."""
    step = TF_SEC["4h"]
    base = BASE - (BASE % step)

    def fake(symbol, tf, start, end, market="spot"):
        if tf == "1m":
            # flat at 99 until minute 25, then dips to 90 (crosses a 95 long limit)
            rows = [{"time": base + i * 60, "open": 99.0, "high": 99.5,
                     "low": (90.0 if i >= 25 else 98.0), "close": 99.0, "volume": 1.0}
                    for i in range(step // 60)]
            return pd.DataFrame(rows)
        return pd.DataFrame()

    monkeypatch.setattr(replay, "fetch_klines", fake)
    df = pd.DataFrame([{"time": base, "open": 99.0, "high": 99.5, "low": 90.0,
                        "close": 99.0, "volume": 1.0}])
    s = replay.Session(id="t", symbol="SOLUSDT", market="spot", tf="4h",
                       start="x", end="y", df=df, cursor=0)
    s.trades = [replay.Trade(id="L", symbol="SOLUSDT", side="long", qty=1.0,
                             order_type="limit", created_time=base, status="pending",
                             limit_price=95.0)]
    s.process_candle()
    assert s.trades[0].status == "open"
    assert s.trades[0].entry_time == base + 25 * 60      # the crossing minute, not `base`


def test_process_candle_only_touches_active_symbol(monkeypatch):
    """A trade tagged for another symbol must not fill/trigger on this symbol's
    candles."""
    monkeypatch.setattr(replay, "fetch_klines",
                        lambda *a, **k: pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"]))
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

"""Write-through Session snapshot persistence.

The in-memory SessionStore stays the source of truth for hot reads; this
module mirrors every mutation to a ReplaySnapshot row so the session survives
worker restarts. On a cold cache miss, hydrate by re-fetching the kline df
for the same range and applying the persisted snapshot.
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from .binance import fetch_klines
from .models import ReplaySnapshot
from .replay import Session, SessionStore


async def save_snapshot(db: AsyncSession, sess: Session) -> None:
    snap = sess.to_snapshot()
    row = await db.get(ReplaySnapshot, sess.id)
    if row is None:
        row = ReplaySnapshot(
            sid=sess.id,
            user_id=sess.user_id or 0,
            symbol=sess.symbol,
            market=sess.market,
            tf=sess.tf,
            is_live=sess.is_live,
            snapshot=snap,
        )
        db.add(row)
    else:
        row.user_id = sess.user_id or row.user_id
        row.symbol = sess.symbol
        row.market = sess.market
        row.tf = sess.tf
        row.is_live = sess.is_live
        row.snapshot = snap
    await db.commit()


async def delete_snapshot(db: AsyncSession, sid: str) -> None:
    row = await db.get(ReplaySnapshot, sid)
    if row is not None:
        await db.delete(row)
        await db.commit()


async def hydrate_session(
    db: AsyncSession, store: SessionStore, sid: str, user_id: int
) -> Session | None:
    """Return a Session for `sid` if it belongs to `user_id`. Checks the in-memory
    store first, falls back to the DB. Returns None if no snapshot exists or it
    belongs to someone else."""
    try:
        sess = store.get(sid)
        if sess.user_id is not None and sess.user_id != user_id:
            return None
        return sess
    except KeyError:
        pass

    row = await db.get(ReplaySnapshot, sid)
    if row is None or row.user_id != user_id:
        return None
    snap = row.snapshot or {}
    symbol = snap.get("symbol", row.symbol)
    market = snap.get("market", row.market)
    tf = snap.get("tf", row.tf)
    start = snap.get("start") or ""
    end = snap.get("end") or ""
    if not start or not end:
        return None
    df = fetch_klines(symbol, tf, start, end, market=market)
    if df.empty:
        return None
    sess = Session(
        id=sid, symbol=symbol, market=market, tf=tf,
        start=start, end=end, df=df,
        cursor=int(snap.get("cursor", 0)),
        user_id=user_id,
        is_live=bool(snap.get("is_live", False)),
    )
    sess.apply_snapshot(snap)
    # Re-derive the cursor: the persisted index can be stale against a freshly
    # fetched df (extend_history prepends bars without widening start), so map
    # by the cursor's candle time when we have it, else clamp into range.
    ct = snap.get("cursor_time")
    if ct is not None:
        idx = int(df["time"].searchsorted(int(ct), side="left"))
        sess.cursor = max(0, min(idx, len(df) - 1))
    else:
        sess.cursor = max(0, min(sess.cursor, len(df) - 1))
    with store._lock:                # noqa: SLF001 — registering hydrated session
        store._sessions[sid] = sess  # noqa: SLF001
    return sess

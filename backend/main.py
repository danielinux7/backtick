"""FastAPI app: serves the frontend and exposes the replay/trade API."""
from __future__ import annotations

import datetime as dt
import os
import secrets
from pathlib import Path

import pandas as pd
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from .aggtrades import fetch_agg_trades, fetch_rest_recent
from .auth import _cookie_kwargs, create_guest, current_user, current_user_optional, is_production
from .binance import TF_MS, VALID_TFS, fetch_klines
from .db import Base, engine, get_db
from .models import User
from .replay import SessionStore, Trade
from .routes_auth import router as auth_router
from .routes_symbols import router as symbols_router
from .snapshots import delete_snapshot, hydrate_session, save_snapshot

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

app = FastAPI(title="Chart Replay")
store = SessionStore()

app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SESSION_SECRET", "dev-only-not-secret-change-me"),
    same_site="lax",
    https_only=os.environ.get("RENDER", "").lower() in {"1", "true"},
)


@app.on_event("startup")
async def _startup() -> None:
    """Auto-create tables on cold start in dev. In production Alembic handles
    migrations as a preDeploy step, but create_all() is idempotent so it's
    safe to keep here for SQLite-first local runs."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


app.include_router(auth_router)
app.include_router(symbols_router)


@app.middleware("http")
async def no_cache(request: Request, call_next):
    """Prevent the browser from serving stale frontend assets during dev."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


class CreateSessionReq(BaseModel):
    symbol: str = Field(..., examples=["SOLUSDT"])
    market: str = Field("spot", pattern="^(spot|futures)$")
    tf: str = Field(..., examples=["4h"])
    start: str | None = None
    end: str | None = None
    warmup: int = 100
    replay_ts: int | None = None   # unix seconds; if set, cursor lands on first candle >= this
    live: bool = False             # true = pull recent klines and stream live updates client-side
    # carry trades over from a previous session (e.g. when the user switches
    # timeframe). Trades use unix timestamps, so they're tf-independent.
    inherit_trades: list[dict] | None = None


class StepReq(BaseModel):
    n: int = 1


class ExtendHistoryReq(BaseModel):
    candles: int = 500


class TradeReq(BaseModel):
    side: str = Field(..., pattern="^(long|short)$")
    qty: float = Field(..., gt=0)
    order_type: str = Field("market", pattern="^(market|limit)$")
    limit_price: float | None = None
    sl: float | None = None
    tp: float | None = None
    at_price: float | None = None      # live mode: explicit reference price for market entry
    at_time: int | None = None         # live mode: unix-seconds timestamp for entry/created time


def _get_agg_trades(sess, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Return aggTrades in [start_ms, end_ms] from the on-disk archive plus the
    session's live-buffer (in live mode), so indicators can see today's data
    that hasn't been published to binance.vision yet."""
    df = fetch_agg_trades(sess.symbol, start_ms, end_ms, sess.market)
    if not sess.is_live or not sess.live_aggtrades:
        return df
    live_rows = [t for t in sess.live_aggtrades
                 if start_ms <= int(t["time_ms"]) <= end_ms]
    if not live_rows:
        return df
    live_df = pd.DataFrame(live_rows)[["time_ms", "price", "qty", "is_buyer_maker"]]
    live_df["time_ms"] = live_df["time_ms"].astype("int64")
    live_df["price"] = live_df["price"].astype("float64")
    live_df["qty"] = live_df["qty"].astype("float64")
    live_df["is_buyer_maker"] = live_df["is_buyer_maker"].astype(bool)
    if df.empty:
        return live_df.sort_values("time_ms").reset_index(drop=True)
    return (pd.concat([df, live_df], ignore_index=True)
            .sort_values("time_ms").reset_index(drop=True))


def _serialize_session(sess) -> dict:
    mark = sess.current_price()
    candles = sess.candles_so_far()
    in_tick = sess.tick_aggs is not None and sess.tick_idx > 0
    return {
        "id": sess.id,
        "symbol": sess.symbol,
        "market": sess.market,
        "tf": sess.tf,
        "start": sess.start,
        "end": sess.end,
        "cursor": sess.cursor,
        "total": int(len(sess.df)),
        "current_time": sess.current_time(),
        "current_price": mark,
        "candles": candles,
        "trades": [t.to_dict(mark) for t in sess.trades],
        "in_tick": in_tick,                                # partial candle present
        "tick_idx": int(sess.tick_idx) if in_tick else 0,
        "is_live": bool(sess.is_live),
    }


async def _resolve(db: AsyncSession, sid: str, user_id: int):
    sess = await hydrate_session(db, store, sid, user_id)
    if sess is None:
        raise HTTPException(404, "session not found")
    return sess


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/api/timeframes")
def timeframes() -> dict:
    # Sort by actual duration (minutes ascending) so the dropdown reads
    # 1m, 3m, 5m, … 1h, 2h, 4h, … 1d, 3d, 1w — not letter-first.
    unit_minutes = {"m": 1, "h": 60, "d": 60 * 24, "w": 60 * 24 * 7}
    return {
        "timeframes": sorted(
            VALID_TFS,
            key=lambda x: int(x[:-1]) * unit_minutes.get(x[-1], 0),
        )
    }


@app.post("/api/session")
async def create_session(
    req: CreateSessionReq,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if req.tf not in VALID_TFS:
        raise HTTPException(400, f"unsupported tf {req.tf}")
    try:
        sess = store.create(req.symbol, req.market, req.tf, req.start, req.end,
                            warmup=req.warmup, replay_ts=req.replay_ts, live=req.live,
                            inherit_trades=req.inherit_trades, user_id=user.id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(502, f"data fetch failed: {e}") from e
    await save_snapshot(db, sess)
    return _serialize_session(sess)


@app.get("/api/session/{sid}")
async def get_session(
    sid: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    sess = await _resolve(db, sid, user.id)
    return _serialize_session(sess)


@app.post("/api/session/{sid}/extend_history")
async def extend_history(
    sid: str, req: ExtendHistoryReq,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    """Prepend older klines to the session so the chart can scroll further back."""
    sess = await _resolve(db, sid, user.id)
    n = max(1, min(req.candles, 5000))
    with sess.lock:
        if sess.df.empty:
            return {"added": 0, "candles": [], "cursor": sess.cursor, "total": int(len(sess.df))}
        first_time = int(sess.df["time"].iloc[0])
        tf_sec = TF_MS[sess.tf] // 1000
        end_ms = first_time * 1000
        # pad by one tf so date-truncated start_iso doesn't cut us short
        start_ms = end_ms - (n + 1) * tf_sec * 1000
        start_iso = dt.datetime.fromtimestamp(start_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
        end_iso = dt.datetime.fromtimestamp(end_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
        try:
            new_df = fetch_klines(sess.symbol, sess.tf, start_iso, end_iso, market=sess.market)
        except Exception as e:
            raise HTTPException(502, f"history fetch failed: {e}") from e
        new_df = new_df[new_df["time"] < first_time].reset_index(drop=True)
        new_df = new_df.tail(n).reset_index(drop=True)
        if new_df.empty:
            return {"added": 0, "candles": [], "cursor": sess.cursor, "total": int(len(sess.df))}
        added = int(len(new_df))
        sess.df = pd.concat([new_df, sess.df], ignore_index=True)
        sess.cursor += added
        candles = [
            {"time": int(r.time), "open": r.open, "high": r.high,
             "low": r.low, "close": r.close, "volume": r.volume}
            for r in new_df.itertuples(index=False)
        ]
    await save_snapshot(db, sess)
    return {"added": added, "candles": candles, "cursor": sess.cursor, "total": int(len(sess.df))}


@app.post("/api/session/{sid}/step")
async def step(
    sid: str, req: StepReq,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    sess = await _resolve(db, sid, user.id)
    with sess.lock:
        changed = []
        n = max(1, min(req.n, 5000))
        if sess.tick_idx > 0 and sess.cursor < len(sess.df) - 1:
            sess.cursor += 1
            sess.reset_tick_state()
            changed.extend(sess.process_candle())
            n -= 1
        for _ in range(n):
            if sess.cursor >= len(sess.df) - 1:
                break
            sess.cursor += 1
            changed.extend(sess.process_candle())
    body = _serialize_session(sess)
    body["changed"] = [t.to_dict(sess.current_price()) for t in changed]
    body["at_end"] = sess.cursor >= len(sess.df) - 1
    await save_snapshot(db, sess)
    return body


@app.post("/api/session/{sid}/tick_step")
async def tick_step(
    sid: str, req: StepReq,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    """Advance n aggTrades (lazily fetched per forming candle). Returns the
    newly-revealed prints so the frontend tape can stream them in."""
    sess = await _resolve(db, sid, user.id)
    with sess.lock:
        n = max(1, min(req.n, 5000))
        new_ticks, changed = sess.step_tick(n)
    body = _serialize_session(sess)
    body["new_ticks"] = new_ticks
    body["changed"] = [t.to_dict(sess.current_price()) for t in changed]
    body["at_end"] = (sess.cursor >= len(sess.df) - 1
                     and (sess.tick_aggs is None or sess.tick_idx >= len(sess.tick_aggs)))
    await save_snapshot(db, sess)
    return body


@app.post("/api/session/{sid}/back")
async def back(
    sid: str, req: StepReq,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    """Rewind cursor. Reopens trades whose fill/exit happened after the new cursor."""
    sess = await _resolve(db, sid, user.id)
    with sess.lock:
        n = max(1, min(req.n, 5000))
        sess.reset_tick_state()
        sess.cursor = max(0, sess.cursor - n)
        new_time = sess.current_time()
        survivors: list[Trade] = []
        for t in sess.trades:
            if t.created_time > new_time:
                continue
            if t.exit_time is not None and t.exit_time > new_time:
                t.exit_time = None
                t.exit_price = None
                t.exit_reason = None
                t.status = "open" if t.entry_time is not None else "pending"
            if t.entry_time is not None and t.entry_time > new_time:
                t.entry_time = None
                t.entry_price = None
                t.status = "pending"
            survivors.append(t)
        sess.trades = survivors
    await save_snapshot(db, sess)
    return _serialize_session(sess)


@app.post("/api/session/{sid}/trade")
async def place_trade(
    sid: str, req: TradeReq,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    sess = await _resolve(db, sid, user.id)
    with sess.lock:
        mark = float(req.at_price) if req.at_price is not None else sess.current_price()

        if req.order_type == "limit":
            if req.limit_price is None or req.limit_price <= 0:
                raise HTTPException(400, "limit_price required for limit order")
            if req.side == "long" and req.limit_price >= mark:
                raise HTTPException(400, "long limit must be below market price")
            if req.side == "short" and req.limit_price <= mark:
                raise HTTPException(400, "short limit must be above market price")
            ref_price = req.limit_price
        else:
            ref_price = mark

        if req.side == "long":
            if req.sl is not None and req.sl >= ref_price:
                raise HTTPException(400, "long SL must be below entry")
            if req.tp is not None and req.tp <= ref_price:
                raise HTTPException(400, "long TP must be above entry")
        else:
            if req.sl is not None and req.sl <= ref_price:
                raise HTTPException(400, "short SL must be above entry")
            if req.tp is not None and req.tp >= ref_price:
                raise HTTPException(400, "short TP must be below entry")

        now_ts = int(req.at_time) if req.at_time is not None else sess.current_time()
        trade = Trade(
            id=secrets.token_hex(4),
            side=req.side,
            qty=req.qty,
            order_type=req.order_type,
            created_time=now_ts,
            sl=req.sl,
            tp=req.tp,
        )
        if req.order_type == "market":
            trade.status = "open"
            trade.entry_time = now_ts
            trade.entry_price = mark
        else:
            trade.status = "pending"
            trade.limit_price = req.limit_price

        sess.trades.append(trade)
    await save_snapshot(db, sess)
    return _serialize_session(sess)


class CloseTradeReq(BaseModel):
    at_price: float | None = None
    at_time: int | None = None


@app.post("/api/session/{sid}/trade/{tid}/close")
async def close_trade(
    sid: str, tid: str, req: CloseTradeReq = CloseTradeReq(),
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    sess = await _resolve(db, sid, user.id)
    with sess.lock:
        for t in sess.trades:
            if t.id == tid and t.status in ("open", "pending"):
                if t.status == "pending":
                    sess.trades = [x for x in sess.trades if x.id != tid]
                else:
                    t.status = "closed"
                    t.exit_time = int(req.at_time) if req.at_time is not None else sess.current_time()
                    t.exit_price = float(req.at_price) if req.at_price is not None else sess.current_price()
                    t.exit_reason = "manual"
                break
        else:
            raise HTTPException(404, "trade not found or already closed")
    await save_snapshot(db, sess)
    return _serialize_session(sess)


@app.get("/api/session/{sid}/footprint")
async def footprint(
    sid: str, from_ts: int, to_ts: int, levels: int = 10,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    """Per-candle buy/sell volume split by price level inside the candle."""
    sess = await _resolve(db, sid, user.id)
    levels = max(4, min(40, levels))
    try:
        df = _get_agg_trades(sess, from_ts * 1000, to_ts * 1000)
    except Exception as e:
        raise HTTPException(502, f"aggTrades fetch failed: {e}") from e
    if df.empty:
        return {"candles": []}
    tf_ms = TF_MS[sess.tf]
    candle_open = ((df["time_ms"].astype("int64") // tf_ms) * tf_ms) // 1000
    df = df.assign(_co=candle_open)
    out = []
    for ct, group in df.groupby("_co"):
        p_min = float(group["price"].min())
        p_max = float(group["price"].max())
        if p_max <= p_min:
            continue
        bucket = (p_max - p_min) / levels
        bin_idx = ((group["price"] - p_min) / bucket).astype("int64").clip(0, levels - 1)
        buy = group["qty"].where(~group["is_buyer_maker"], 0.0)
        sell = group["qty"].where(group["is_buyer_maker"], 0.0)
        agg = pd.DataFrame({"bin": bin_idx, "buy": buy, "sell": sell}) \
            .groupby("bin")[["buy", "sell"]].sum()
        lvls = []
        for i in range(levels):
            if i in agg.index:
                b = float(agg.loc[i, "buy"]); s = float(agg.loc[i, "sell"])
            else:
                b = s = 0.0
            if b == 0 and s == 0:
                continue
            lvls.append({
                "price_low": p_min + i * bucket,
                "price_high": p_min + (i + 1) * bucket,
                "buy": b, "sell": s,
            })
        if lvls:
            out.append({"time": int(ct), "levels": lvls})
    return {"candles": out}


@app.get("/api/session/{sid}/liquidations")
async def liquidations(
    sid: str, from_ts: int, to_ts: int,
    percentile: float = 0.995, min_qty: float = 0.0,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    sess = await _resolve(db, sid, user.id)
    percentile = max(0.5, min(0.9999, percentile))
    try:
        df = _get_agg_trades(sess, from_ts * 1000, to_ts * 1000)
    except Exception as e:
        raise HTTPException(502, f"aggTrades fetch failed: {e}") from e
    if df.empty:
        return {"events": [], "threshold": 0.0}
    threshold = max(float(df["qty"].quantile(percentile)), float(min_qty))
    outliers = df[df["qty"] >= threshold]
    events = [
        {
            "time_ms": int(r.time_ms),
            "price": float(r.price),
            "qty": float(r.qty),
            "side": "sell" if bool(r.is_buyer_maker) else "buy",
        }
        for r in outliers.itertuples(index=False)
    ]
    return {"events": events, "threshold": threshold}


@app.get("/api/session/{sid}/vol_profile")
async def vol_profile(
    sid: str, from_ts: int, to_ts: int, buckets: int = 40,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    sess = await _resolve(db, sid, user.id)
    buckets = max(8, min(120, buckets))
    try:
        df = _get_agg_trades(sess, from_ts * 1000, to_ts * 1000)
    except Exception as e:
        raise HTTPException(502, f"aggTrades fetch failed: {e}") from e
    if df.empty:
        return {"buckets": [], "max_vol": 0.0, "price_min": 0.0, "price_max": 0.0, "poc_idx": -1}
    p_min = float(df["price"].min())
    p_max = float(df["price"].max())
    if p_max <= p_min:
        return {"buckets": [], "max_vol": 0.0, "price_min": p_min, "price_max": p_max, "poc_idx": -1}
    bucket_size = (p_max - p_min) / buckets
    bin_idx = ((df["price"] - p_min) / bucket_size).astype("int64").clip(0, buckets - 1)
    buy_qty = df["qty"].where(~df["is_buyer_maker"], 0.0)
    sell_qty = df["qty"].where(df["is_buyer_maker"], 0.0)
    grouped = pd.DataFrame({"bin": bin_idx, "buy": buy_qty, "sell": sell_qty}) \
        .groupby("bin")[["buy", "sell"]].sum()
    out = []
    max_vol = 0.0
    poc_idx = 0
    for i in range(buckets):
        if i in grouped.index:
            b = float(grouped.loc[i, "buy"]); s = float(grouped.loc[i, "sell"])
        else:
            b = s = 0.0
        total = b + s
        if total > max_vol:
            max_vol = total; poc_idx = i
        out.append({
            "price_low": p_min + i * bucket_size,
            "price_high": p_min + (i + 1) * bucket_size,
            "buy": b, "sell": s,
        })
    return {
        "buckets": out, "max_vol": max_vol,
        "price_min": p_min, "price_max": p_max,
        "poc_idx": poc_idx,
    }


@app.get("/api/session/{sid}/cvd")
async def cvd(
    sid: str,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    sess = await _resolve(db, sid, user.id)
    with sess.lock:
        end_idx = sess.cursor
        if end_idx < 0:
            return {"points": []}
        tf_ms = TF_MS[sess.tf]
        candle_times = [int(sess.df["time"].iloc[i]) for i in range(end_idx + 1)]
        missing = [t for t in candle_times if t not in sess.cvd_cache]
        if missing:
            start_ms = min(missing) * 1000
            end_ms = max(missing) * 1000 + tf_ms
            try:
                df = _get_agg_trades(sess, start_ms, end_ms)
            except Exception as e:
                raise HTTPException(502, f"aggTrades fetch failed: {e}") from e
            if df.empty:
                for t in missing:
                    sess.cvd_cache[t] = {"delta": 0.0, "max_rel": 0.0, "min_rel": 0.0}
            else:
                df = df.sort_values("time_ms").reset_index(drop=True)
                signed = df["qty"].where(~df["is_buyer_maker"], -df["qty"]).astype("float64")
                base_ms = min(missing) * 1000
                bins = ((df["time_ms"].astype("int64") - base_ms) // tf_ms).astype("int64")
                grouped = signed.groupby(bins)
                stats = grouped.agg([
                    ("delta", "sum"),
                    ("max_rel", lambda s: float(s.cumsum().max())),
                    ("min_rel", lambda s: float(s.cumsum().min())),
                ])
                for t in missing:
                    bin_idx = (t * 1000 - base_ms) // tf_ms
                    if bin_idx in stats.index:
                        row = stats.loc[bin_idx]
                        sess.cvd_cache[t] = {
                            "delta": float(row["delta"]),
                            "max_rel": float(row["max_rel"]),
                            "min_rel": float(row["min_rel"]),
                        }
                    else:
                        sess.cvd_cache[t] = {"delta": 0.0, "max_rel": 0.0, "min_rel": 0.0}
        cum = 0.0
        points = []
        for t in candle_times:
            info = sess.cvd_cache.get(t) or {"delta": 0.0, "max_rel": 0.0, "min_rel": 0.0}
            open_v = cum
            close_v = cum + info["delta"]
            high_v = cum + info["max_rel"]
            low_v = cum + info["min_rel"]
            cum = close_v
            points.append({
                "time": t,
                "open": open_v,
                "high": high_v,
                "low": low_v,
                "close": close_v,
            })
    return {"points": points}


@app.get("/api/session/{sid}/recent_trades")
async def recent_trades(
    sid: str, n: int = 60,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    sess = await _resolve(db, sid, user.id)
    n = max(1, min(n, 500))
    tf_ms = TF_MS[sess.tf]
    if sess.is_live:
        try:
            df = fetch_rest_recent(sess.symbol, sess.market, limit=max(n, 200))
        except Exception as e:
            raise HTTPException(502, f"aggTrades fetch failed: {e}") from e
    else:
        end_ms = sess.current_time() * 1000 + tf_ms
        for window_candles in (1, 3, 10):
            start_ms = end_ms - tf_ms * window_candles
            try:
                df = _get_agg_trades(sess, start_ms, end_ms)
            except Exception as e:
                raise HTTPException(502, f"aggTrades fetch failed: {e}") from e
            if len(df) >= n or window_candles == 10:
                break
    df = df.tail(n)
    return {
        "trades": [
            {
                "time_ms": int(r.time_ms),
                "price": float(r.price),
                "qty": float(r.qty),
                "side": "sell" if r.is_buyer_maker else "buy",
            }
            for r in df.itertuples(index=False)
        ],
    }


class PushTicksReq(BaseModel):
    ticks: list[dict]


class PushKlineReq(BaseModel):
    time: int           # candle open time, seconds
    open: float
    high: float
    low: float
    close: float
    volume: float


@app.post("/api/session/{sid}/push_ticks")
async def push_ticks(
    sid: str, req: PushTicksReq,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    sess = await _resolve(db, sid, user.id)
    if not sess.is_live:
        return {"buffered": 0, "changed": []}
    changed: list = []
    seen: set[str] = set()
    with sess.lock:
        sess.live_aggtrades.extend(req.ticks)
        cutoff = (sess.df["time"].iloc[0] * 1000) if len(sess.df) else 0
        if cutoff:
            sess.live_aggtrades = [t for t in sess.live_aggtrades
                                   if int(t["time_ms"]) >= cutoff]
        tf_s = TF_MS[sess.tf] // 1000
        for t in req.ticks:
            ms = int(t["time_ms"])
            price = float(t["price"])
            candle_open = (ms // (tf_s * 1000)) * tf_s
            sess.cvd_cache.pop(candle_open, None)
            for c in sess._process_tick(price, ms):
                if c.id not in seen:
                    seen.add(c.id); changed.append(c)
        mark = float(req.ticks[-1]["price"]) if req.ticks else sess.current_price()
    if changed:
        await save_snapshot(db, sess)
    return {
        "buffered": len(sess.live_aggtrades),
        "changed": [t.to_dict(mark) for t in changed],
    }


@app.post("/api/session/{sid}/push_kline")
async def push_kline(
    sid: str, req: PushKlineReq,
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    sess = await _resolve(db, sid, user.id)
    if not sess.is_live:
        return {"appended": False}
    with sess.lock:
        last_time = int(sess.df["time"].iloc[-1]) if len(sess.df) else 0
        if req.time <= last_time:
            return {"appended": False}
        new_row = pd.DataFrame([{
            "time": int(req.time),
            "open": float(req.open),
            "high": float(req.high),
            "low": float(req.low),
            "close": float(req.close),
            "volume": float(req.volume),
        }])
        sess.df = pd.concat([sess.df, new_row], ignore_index=True)
        sess.cursor = len(sess.df) - 1
        sess.cvd_cache.pop(int(req.time), None)
    await save_snapshot(db, sess)
    return {"appended": True, "cursor": sess.cursor}


@app.delete("/api/session/{sid}")
async def delete_session(
    sid: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete from both the in-memory store and the persisted snapshot.
    Validates ownership at BOTH layers — without the DB check, a cold-cache
    snapshot belonging to another user could be deleted by anyone with its sid."""
    in_memory_ok = False
    try:
        sess = store.get(sid)
        if sess.user_id is not None and sess.user_id != user.id:
            raise HTTPException(404, "session not found")
        in_memory_ok = True
    except KeyError:
        pass
    from .models import ReplaySnapshot
    row = await db.get(ReplaySnapshot, sid)
    if row is not None and row.user_id != user.id:
        raise HTTPException(404, "session not found")
    if not in_memory_ok and row is None:
        raise HTTPException(404, "session not found")
    store.delete(sid)
    await delete_snapshot(db, sid)
    return {"ok": True}


@app.get("/")
async def index(
    user: User | None = Depends(current_user_optional),
    db: AsyncSession = Depends(get_db),
):
    """Chart-first landing: no auth wall. If the visitor has no auth cookie,
    auto-create an anonymous guest user so they can immediately try the app.
    They can sign in or sign up later via the header link."""
    resp = FileResponse(FRONTEND / "index.html")
    if user is None:
        _, token = await create_guest(db)
        resp.set_cookie(value=token, **_cookie_kwargs(secure=is_production()))
    return resp


@app.get("/login")
def login_page() -> FileResponse:
    return FileResponse(FRONTEND / "login.html")


# Service workers can only intercept requests inside their served scope, so
# /sw.js MUST live at the site root, not under /static. Same for the manifest
# (so iOS Safari + Android Chrome find it from a normalized path).
@app.get("/sw.js")
def service_worker() -> FileResponse:
    return FileResponse(
        FRONTEND / "sw.js",
        media_type="text/javascript",
        headers={"Cache-Control": "no-store, must-revalidate", "Service-Worker-Allowed": "/"},
    )


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    return FileResponse(
        FRONTEND / "manifest.webmanifest",
        media_type="application/manifest+json",
    )


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

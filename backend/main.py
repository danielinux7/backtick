"""FastAPI app: serves the frontend and exposes the replay/trade API."""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .aggtrades import fetch_agg_trades
from .binance import TF_MS, VALID_TFS
from .replay import SessionStore, Trade

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

app = FastAPI(title="Chart Replay")
store = SessionStore()


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
    start: str
    end: str
    warmup: int = 100
    replay_ts: int | None = None   # unix seconds; if set, cursor lands on first candle >= this


class StepReq(BaseModel):
    n: int = 1


class TradeReq(BaseModel):
    side: str = Field(..., pattern="^(long|short)$")
    qty: float = Field(..., gt=0)
    order_type: str = Field("market", pattern="^(market|limit)$")
    limit_price: float | None = None
    sl: float | None = None
    tp: float | None = None


def _serialize_session(sess) -> dict:
    mark = sess.current_price()
    candles = sess.candles_so_far()
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
    }


@app.get("/api/timeframes")
def timeframes() -> dict:
    return {"timeframes": sorted(VALID_TFS, key=lambda x: (x[-1], int(x[:-1])))}


@app.post("/api/session")
def create_session(req: CreateSessionReq) -> dict:
    if req.tf not in VALID_TFS:
        raise HTTPException(400, f"unsupported tf {req.tf}")
    try:
        sess = store.create(req.symbol, req.market, req.tf, req.start, req.end,
                            warmup=req.warmup, replay_ts=req.replay_ts)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(502, f"data fetch failed: {e}") from e
    return _serialize_session(sess)


@app.get("/api/session/{sid}")
def get_session(sid: str) -> dict:
    try:
        sess = store.get(sid)
    except KeyError:
        raise HTTPException(404, "session not found")
    return _serialize_session(sess)


@app.post("/api/session/{sid}/step")
def step(sid: str, req: StepReq) -> dict:
    try:
        sess = store.get(sid)
    except KeyError:
        raise HTTPException(404, "session not found")
    with sess.lock:
        changed = []
        n = max(1, min(req.n, 5000))
        for _ in range(n):
            if sess.cursor >= len(sess.df) - 1:
                break
            sess.cursor += 1
            changed.extend(sess.process_candle())
    body = _serialize_session(sess)
    body["changed"] = [t.to_dict(sess.current_price()) for t in changed]
    body["at_end"] = sess.cursor >= len(sess.df) - 1
    return body


@app.post("/api/session/{sid}/back")
def back(sid: str, req: StepReq) -> dict:
    """Rewind cursor. Reopens trades whose fill/exit happened after the new cursor."""
    try:
        sess = store.get(sid)
    except KeyError:
        raise HTTPException(404, "session not found")
    with sess.lock:
        n = max(1, min(req.n, 5000))
        sess.cursor = max(0, sess.cursor - n)
        new_time = sess.current_time()
        survivors: list[Trade] = []
        for t in sess.trades:
            # drop trades submitted in the (now) future
            if t.created_time > new_time:
                continue
            # un-close trades whose exit was in the future
            if t.exit_time is not None and t.exit_time > new_time:
                t.exit_time = None
                t.exit_price = None
                t.exit_reason = None
                t.status = "open" if t.entry_time is not None else "pending"
            # un-fill trades whose entry was in the future
            if t.entry_time is not None and t.entry_time > new_time:
                t.entry_time = None
                t.entry_price = None
                t.status = "pending"
            survivors.append(t)
        sess.trades = survivors
    return _serialize_session(sess)


@app.post("/api/session/{sid}/trade")
def place_trade(sid: str, req: TradeReq) -> dict:
    try:
        sess = store.get(sid)
    except KeyError:
        raise HTTPException(404, "session not found")
    with sess.lock:
        mark = sess.current_price()

        if req.order_type == "limit":
            if req.limit_price is None or req.limit_price <= 0:
                raise HTTPException(400, "limit_price required for limit order")
            # require limit on the correct side of market price
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

        trade = Trade(
            id=secrets.token_hex(4),
            side=req.side,
            qty=req.qty,
            order_type=req.order_type,
            created_time=sess.current_time(),
            sl=req.sl,
            tp=req.tp,
        )
        if req.order_type == "market":
            trade.status = "open"
            trade.entry_time = sess.current_time()
            trade.entry_price = mark
        else:
            trade.status = "pending"
            trade.limit_price = req.limit_price

        sess.trades.append(trade)
    return _serialize_session(sess)


@app.post("/api/session/{sid}/trade/{tid}/close")
def close_trade(sid: str, tid: str) -> dict:
    try:
        sess = store.get(sid)
    except KeyError:
        raise HTTPException(404, "session not found")
    with sess.lock:
        for t in sess.trades:
            if t.id == tid and t.status in ("open", "pending"):
                if t.status == "pending":
                    # cancel the order
                    sess.trades = [x for x in sess.trades if x.id != tid]
                else:
                    t.status = "closed"
                    t.exit_time = sess.current_time()
                    t.exit_price = sess.current_price()
                    t.exit_reason = "manual"
                break
        else:
            raise HTTPException(404, "trade not found or already closed")
    return _serialize_session(sess)


@app.get("/api/session/{sid}/recent_trades")
def recent_trades(sid: str, n: int = 60) -> dict:
    """Return the most recent N aggTrades up to (and including) the candle
    currently revealed by the cursor. Used by the Time & Sales panel."""
    try:
        sess = store.get(sid)
    except KeyError:
        raise HTTPException(404, "session not found")
    n = max(1, min(n, 500))
    tf_ms = TF_MS[sess.tf]
    end_ms = sess.current_time() * 1000 + tf_ms                 # end of cursor candle
    # widen the lookback window if the symbol is illiquid — try up to ~10 candles
    for window_candles in (1, 3, 10):
        start_ms = end_ms - tf_ms * window_candles
        try:
            df = fetch_agg_trades(sess.symbol, start_ms, end_ms, sess.market)
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


@app.delete("/api/session/{sid}")
def delete_session(sid: str) -> dict:
    store.delete(sid)
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

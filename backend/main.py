"""FastAPI app: serves the frontend and exposes the replay/trade API."""
from __future__ import annotations

import secrets
from pathlib import Path

import pandas as pd
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
        # if a tick-mode replay had a partial candle in flight, finalize it
        # via the kline before stepping further
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
    return body


@app.post("/api/session/{sid}/tick_step")
def tick_step(sid: str, req: StepReq) -> dict:
    """Advance n aggTrades (lazily fetched per forming candle). Returns the
    newly-revealed prints so the frontend tape can stream them in."""
    try:
        sess = store.get(sid)
    except KeyError:
        raise HTTPException(404, "session not found")
    with sess.lock:
        n = max(1, min(req.n, 5000))
        new_ticks, changed = sess.step_tick(n)
    body = _serialize_session(sess)
    body["new_ticks"] = new_ticks
    body["changed"] = [t.to_dict(sess.current_price()) for t in changed]
    body["at_end"] = (sess.cursor >= len(sess.df) - 1
                     and (sess.tick_aggs is None or sess.tick_idx >= len(sess.tick_aggs)))
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
        sess.reset_tick_state()       # rewind cancels any partial-candle tick replay
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


@app.get("/api/session/{sid}/liquidations")
def liquidations(sid: str, from_ts: int, to_ts: int,
                 percentile: float = 0.995, min_qty: float = 0.0) -> dict:
    """Heuristic liquidation candidates — aggTrade prints whose qty sits in the
    top 1 - percentile of the visible window. Not real liquidation data
    (Binance deprecated /allForceOrders in 2021), but large taker prints are
    correlated with liquidation cascades and whale exits."""
    try:
        sess = store.get(sid)
    except KeyError:
        raise HTTPException(404, "session not found")
    percentile = max(0.5, min(0.9999, percentile))
    try:
        df = fetch_agg_trades(sess.symbol, from_ts * 1000, to_ts * 1000, sess.market)
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
def vol_profile(sid: str, from_ts: int, to_ts: int, buckets: int = 40) -> dict:
    """Aggregate aggTrades inside [from_ts, to_ts] into a volume-by-price
    histogram with taker-buy / taker-sell split per bucket."""
    try:
        sess = store.get(sid)
    except KeyError:
        raise HTTPException(404, "session not found")
    buckets = max(8, min(120, buckets))
    try:
        df = fetch_agg_trades(sess.symbol, from_ts * 1000, to_ts * 1000, sess.market)
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
def cvd(sid: str) -> dict:
    """Per-candle OHLC of cumulative volume delta — taker buy minus taker sell,
    with intra-candle min/max so the chart can render proper candlesticks.
    Heavy on first call for a session (pulls aggTrades for the revealed range);
    subsequent calls reuse the per-candle cache."""
    try:
        sess = store.get(sid)
    except KeyError:
        raise HTTPException(404, "session not found")
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
                df = fetch_agg_trades(sess.symbol, start_ms, end_ms, sess.market)
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
                # group + compute candle-local cumulative OHLC stats
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

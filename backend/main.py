"""FastAPI app: serves the frontend and exposes the replay/trade API."""
from __future__ import annotations

import datetime as dt
import secrets
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .aggtrades import fetch_agg_trades, fetch_rest_recent
from .binance import TF_MS, VALID_TFS, fetch_klines
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


@app.get("/api/timeframes")
def timeframes() -> dict:
    return {"timeframes": sorted(VALID_TFS, key=lambda x: (x[-1], int(x[:-1])))}


@app.post("/api/session")
def create_session(req: CreateSessionReq) -> dict:
    if req.tf not in VALID_TFS:
        raise HTTPException(400, f"unsupported tf {req.tf}")
    try:
        sess = store.create(req.symbol, req.market, req.tf, req.start, req.end,
                            warmup=req.warmup, replay_ts=req.replay_ts, live=req.live,
                            inherit_trades=req.inherit_trades)
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


@app.post("/api/session/{sid}/extend_history")
def extend_history(sid: str, req: ExtendHistoryReq) -> dict:
    """Prepend older klines to the session so the chart can scroll further back."""
    try:
        sess = store.get(sid)
    except KeyError:
        raise HTTPException(404, "session not found")
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
        # tail(n) so we return at most n bars even if the cached range pulled more
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
    return {"added": added, "candles": candles, "cursor": sess.cursor, "total": int(len(sess.df))}


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
        # in live mode the chart's "now" price is owned by the frontend WS feed,
        # so honor at_price if it was supplied
        mark = float(req.at_price) if req.at_price is not None else sess.current_price()

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

        # in live mode the frontend supplies the actual "now" timestamp;
        # sess.current_time() is the last completed kline's open, which is the
        # PREVIOUS candle and would mis-place markers.
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
            trade.entry_price = mark        # uses at_price in live mode
        else:
            trade.status = "pending"
            trade.limit_price = req.limit_price

        sess.trades.append(trade)
    return _serialize_session(sess)


class CloseTradeReq(BaseModel):
    at_price: float | None = None
    at_time: int | None = None


@app.post("/api/session/{sid}/trade/{tid}/close")
def close_trade(sid: str, tid: str, req: CloseTradeReq = CloseTradeReq()) -> dict:
    try:
        sess = store.get(sid)
    except KeyError:
        raise HTTPException(404, "session not found")
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
    return _serialize_session(sess)


@app.get("/api/session/{sid}/footprint")
def footprint(sid: str, from_ts: int, to_ts: int, levels: int = 10) -> dict:
    """Per-candle buy/sell volume split by price level inside the candle. Used
    by the footprint overlay — only meaningful when the chart is zoomed in far
    enough that each candle has decent horizontal space."""
    try:
        sess = store.get(sid)
    except KeyError:
        raise HTTPException(404, "session not found")
    levels = max(4, min(40, levels))
    try:
        df = _get_agg_trades(sess, from_ts * 1000, to_ts * 1000)
    except Exception as e:
        raise HTTPException(502, f"aggTrades fetch failed: {e}") from e
    if df.empty:
        return {"candles": []}
    tf_ms = TF_MS[sess.tf]
    # which candle does each trade belong to (open time, seconds)
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
def vol_profile(sid: str, from_ts: int, to_ts: int, buckets: int = 40) -> dict:
    """Aggregate aggTrades inside [from_ts, to_ts] into a volume-by-price
    histogram with taker-buy / taker-sell split per bucket."""
    try:
        sess = store.get(sid)
    except KeyError:
        raise HTTPException(404, "session not found")
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
    if sess.is_live:
        # Vision archive doesn't carry today, and _get_agg_trades returns empty
        # for today to avoid burning the REST rate limit. Hit /aggTrades directly
        # for the most recent prints so the tape lands right at 'now' — no gap
        # between the last closed-candle prints and the live WS feed.
        try:
            df = fetch_rest_recent(sess.symbol, sess.market, limit=max(n, 200))
        except Exception as e:
            raise HTTPException(502, f"aggTrades fetch failed: {e}") from e
    else:
        end_ms = sess.current_time() * 1000 + tf_ms             # end of cursor candle
        # widen the lookback window if the symbol is illiquid — try up to ~10 candles
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
def push_ticks(sid: str, req: PushTicksReq) -> dict:
    """Frontend streams live aggTrades here (in live mode) so indicators that
    aggregate aggTrades can see today's data. Each tick: {time_ms, price, qty,
    is_buyer_maker}. We also invalidate CVD cache for the candles those ticks
    touch so the running cumulative recomputes."""
    try:
        sess = store.get(sid)
    except KeyError:
        raise HTTPException(404, "session not found")
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
        # process each tick: invalidate CVD cache + run limit/SL/TP per tick
        for t in req.ticks:
            ms = int(t["time_ms"])
            price = float(t["price"])
            candle_open = (ms // (tf_s * 1000)) * tf_s
            sess.cvd_cache.pop(candle_open, None)
            for c in sess._process_tick(price, ms):
                if c.id not in seen:
                    seen.add(c.id); changed.append(c)
        mark = float(req.ticks[-1]["price"]) if req.ticks else sess.current_price()
    return {
        "buffered": len(sess.live_aggtrades),
        "changed": [t.to_dict(mark) for t in changed],
    }


@app.post("/api/session/{sid}/push_kline")
def push_kline(sid: str, req: PushKlineReq) -> dict:
    """Frontend tells the backend about a newly-closed kline so indicator
    endpoints can include it in their per-candle aggregation."""
    try:
        sess = store.get(sid)
    except KeyError:
        raise HTTPException(404, "session not found")
    if not sess.is_live:
        return {"appended": False}
    with sess.lock:
        last_time = int(sess.df["time"].iloc[-1]) if len(sess.df) else 0
        if req.time <= last_time:
            return {"appended": False}      # already known or stale
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
        # the newly-closed candle's CVD should be (re)computed from buffered ticks
        sess.cvd_cache.pop(int(req.time), None)
    return {"appended": True, "cursor": sess.cursor}


@app.delete("/api/session/{sid}")
def delete_session(sid: str) -> dict:
    store.delete(sid)
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

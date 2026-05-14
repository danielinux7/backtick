"""In-memory replay sessions: candles, cursor, trades, P&L."""
from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass, field
from threading import RLock

import pandas as pd

from .aggtrades import fetch_agg_trades
from .binance import TF_MS, fetch_klines


TradeStatus = str  # "pending" | "open" | "closed"


@dataclass
class Trade:
    id: str
    side: str               # "long" | "short"
    qty: float
    order_type: str         # "market" | "limit"
    created_time: int       # unix seconds at submission
    status: TradeStatus = "pending"
    limit_price: float | None = None
    entry_time: int | None = None
    entry_price: float | None = None
    sl: float | None = None
    tp: float | None = None
    exit_time: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None   # "manual" | "sl" | "tp"

    def pnl(self, mark: float) -> float:
        if self.status == "pending" or self.entry_price is None:
            return 0.0
        if self.status == "closed":
            assert self.exit_price is not None
            mark = self.exit_price
        diff = mark - self.entry_price
        if self.side == "short":
            diff = -diff
        return diff * self.qty

    def to_dict(self, mark: float | None = None) -> dict:
        return {
            "id": self.id,
            "side": self.side,
            "qty": self.qty,
            "order_type": self.order_type,
            "status": self.status,
            "limit_price": self.limit_price,
            "created_time": self.created_time,
            "entry_time": self.entry_time,
            "entry_price": self.entry_price,
            "sl": self.sl,
            "tp": self.tp,
            "exit_time": self.exit_time,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "pnl": self.pnl(mark) if mark is not None else None,
        }


@dataclass
class Session:
    id: str
    symbol: str
    market: str
    tf: str
    start: str
    end: str
    df: pd.DataFrame
    cursor: int                              # index of last FULLY revealed candle (0-based)
    trades: list[Trade] = field(default_factory=list)
    lock: RLock = field(default_factory=RLock)
    # tick-replay state: aggTrades for the FORMING candle (cursor+1), and how
    # many of those trades have already been revealed.
    tick_aggs: pd.DataFrame | None = None
    tick_idx: int = 0
    is_live: bool = False
    # live-mode buffer of aggTrades posted by the frontend WebSocket stream;
    # each entry: {time_ms, price, qty, is_buyer_maker}
    live_aggtrades: list[dict] = field(default_factory=list)
    # per-candle CVD stats; each value is
    #   {"delta": net signed qty, "max_rel": peak intra-candle cum, "min_rel": trough}
    # — recorded relative to the start of the candle so we can stitch a running
    # cumulative OHLC line at request time without re-walking aggTrades
    cvd_cache: dict[int, dict] = field(default_factory=dict)

    def candles_so_far(self) -> list[dict]:
        sub = self.df.iloc[: self.cursor + 1]
        out = [
            {"time": int(r.time), "open": r.open, "high": r.high,
             "low": r.low, "close": r.close, "volume": r.volume}
            for r in sub.itertuples(index=False)
        ]
        partial = self.partial_candle()
        if partial is not None:
            out.append(partial)
        return out

    def partial_candle(self) -> dict | None:
        """OHLC of the not-yet-completed candle, built from revealed ticks. None
        if we're not currently mid-candle (tick_idx == 0 or no agg data)."""
        if self.tick_aggs is None or self.tick_idx == 0:
            return None
        if self.cursor + 1 >= len(self.df):
            return None
        revealed = self.tick_aggs.iloc[: self.tick_idx]
        if revealed.empty:
            return None
        next_kline = self.df.iloc[self.cursor + 1]
        prices = revealed["price"]
        return {
            "time": int(next_kline["time"]),
            "open": float(prices.iloc[0]),
            "high": float(prices.max()),
            "low": float(prices.min()),
            "close": float(prices.iloc[-1]),
            "volume": float(revealed["qty"].sum()),
        }

    def current_price(self) -> float:
        if self.tick_aggs is not None and self.tick_idx > 0:
            return float(self.tick_aggs.iloc[self.tick_idx - 1]["price"])
        return float(self.df["close"].iloc[self.cursor])

    def current_time(self) -> int:
        if self.tick_aggs is not None and self.tick_idx > 0:
            return int(self.tick_aggs.iloc[self.tick_idx - 1]["time_ms"] // 1000)
        return int(self.df["time"].iloc[self.cursor])

    def _load_tick_aggs(self) -> None:
        if self.cursor + 1 >= len(self.df):
            self.tick_aggs = None
            self.tick_idx = 0
            return
        next_kline = self.df.iloc[self.cursor + 1]
        start_ms = int(next_kline["time"]) * 1000
        end_ms = start_ms + TF_MS[self.tf]
        try:
            df = fetch_agg_trades(self.symbol, start_ms, end_ms, self.market)
        except Exception:
            df = pd.DataFrame(columns=["time_ms", "price", "qty", "is_buyer_maker"])
        self.tick_aggs = df.reset_index(drop=True)
        self.tick_idx = 0

    def reset_tick_state(self) -> None:
        self.tick_aggs = None
        self.tick_idx = 0

    def step_tick(self, n: int) -> tuple[list[dict], list[Trade]]:
        """Reveal the next n aggTrades, processing limits / SL / TP per tick.
        Auto-advances the kline cursor whenever a forming candle's ticks are
        exhausted (and lazily fetches the next candle's aggTrades)."""
        new_ticks: list[dict] = []
        changed: list[Trade] = []
        seen: set[str] = set()

        remaining = n
        while remaining > 0 and self.cursor < len(self.df) - 1:
            if self.tick_aggs is None:
                self._load_tick_aggs()
            if self.tick_aggs is None or self.tick_aggs.empty:
                # symbol+candle has no aggTrades available — fall back to
                # candle-level processing and move on
                self.cursor += 1
                self.tick_aggs = None
                self.tick_idx = 0
                for c in self.process_candle():
                    if c.id not in seen:
                        seen.add(c.id); changed.append(c)
                continue
            avail = len(self.tick_aggs) - self.tick_idx
            if avail == 0:
                # forming candle is done — promote it and load the next one
                self.cursor += 1
                self.tick_aggs = None
                self.tick_idx = 0
                continue
            take = min(remaining, avail)
            for i in range(take):
                t = self.tick_aggs.iloc[self.tick_idx + i]
                price = float(t["price"])
                time_ms = int(t["time_ms"])
                new_ticks.append({
                    "time_ms": time_ms,
                    "price": price,
                    "qty": float(t["qty"]),
                    "side": "sell" if bool(t["is_buyer_maker"]) else "buy",
                })
                for c in self._process_tick(price, time_ms):
                    if c.id not in seen:
                        seen.add(c.id); changed.append(c)
            self.tick_idx += take
            remaining -= take
        return new_ticks, changed

    def _process_tick(self, price: float, time_ms: int) -> list[Trade]:
        """Per-tick limit fill + SL/TP check. Much sharper than candle-level
        because we know the exact intra-candle ordering of prints."""
        time_s = time_ms // 1000
        changed: list[Trade] = []
        just_filled: set[str] = set()
        for t in self.trades:
            if t.status != "pending" or t.limit_price is None:
                continue
            if t.side == "long" and price <= t.limit_price:
                ok = True
            elif t.side == "short" and price >= t.limit_price:
                ok = True
            else:
                ok = False
            if ok:
                t.status = "open"
                t.entry_time = time_s
                t.entry_price = t.limit_price
                just_filled.add(t.id)
                changed.append(t)
        for t in self.trades:
            if t.status != "open" or t.sl is None or t.id in just_filled:
                continue
            hit: float | None = None
            reason: str | None = None
            if t.side == "long":
                if price <= t.sl:
                    hit, reason = t.sl, "sl"
                elif t.tp is not None and price >= t.tp:
                    hit, reason = t.tp, "tp"
            else:
                if price >= t.sl:
                    hit, reason = t.sl, "sl"
                elif t.tp is not None and price <= t.tp:
                    hit, reason = t.tp, "tp"
            if hit is not None:
                t.status = "closed"
                t.exit_time = time_s
                t.exit_price = hit
                t.exit_reason = reason
                changed.append(t)
        return changed

    def process_candle(self) -> list[Trade]:
        """Fill pending limits, then trigger SL/TP. Returns trades whose state changed.

        - Limit fills use the candle open if it gapped past the limit (realistic gap fill).
        - SL/TP also gap-fill at the open when the candle opens past the trigger.
        - A trade that just filled this candle is *not* eligible for SL/TP this same
          candle: we can't know the intra-candle order of price moves, so claiming
          a same-bar TP/SL hit is unreliable. Eligibility resumes next candle.
        """
        if self.cursor < 0:
            return []
        row = self.df.iloc[self.cursor]
        op, hi, lo = float(row.open), float(row.high), float(row.low)
        rt = int(row.time)
        changed: list[Trade] = []

        # 1) fill pending limit orders
        just_filled: set[str] = set()
        for t in self.trades:
            if t.status != "pending" or t.limit_price is None:
                continue
            entry: float | None = None
            if t.side == "long":
                if op <= t.limit_price:
                    entry = op                    # gap-down through the limit
                elif lo <= t.limit_price:
                    entry = t.limit_price         # intra-candle touch
            else:
                if op >= t.limit_price:
                    entry = op                    # gap-up through the limit
                elif hi >= t.limit_price:
                    entry = t.limit_price
            if entry is not None:
                t.status = "open"
                t.entry_time = rt
                t.entry_price = entry
                just_filled.add(t.id)
                changed.append(t)

        # 2) SL/TP on open trades — only when SL is set, and never on the same
        # candle that filled the trade.
        for t in self.trades:
            if t.status != "open" or t.sl is None or t.id in just_filled:
                continue
            hit_price: float | None = None
            reason: str | None = None
            if t.side == "long":
                if op <= t.sl:
                    hit_price, reason = op, "sl"          # gap-down past SL
                elif t.tp is not None and op >= t.tp:
                    hit_price, reason = op, "tp"          # gap-up past TP
                elif lo <= t.sl:
                    hit_price, reason = t.sl, "sl"
                elif t.tp is not None and hi >= t.tp:
                    hit_price, reason = t.tp, "tp"
            else:
                if op >= t.sl:
                    hit_price, reason = op, "sl"          # gap-up past SL
                elif t.tp is not None and op <= t.tp:
                    hit_price, reason = op, "tp"          # gap-down past TP
                elif hi >= t.sl:
                    hit_price, reason = t.sl, "sl"
                elif t.tp is not None and lo <= t.tp:
                    hit_price, reason = t.tp, "tp"
            if hit_price is not None:
                t.status = "closed"
                t.exit_time = rt
                t.exit_price = hit_price
                t.exit_reason = reason
                changed.append(t)

        return changed


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = RLock()

    def create(self, symbol: str, market: str, tf: str,
               start: str | None = None, end: str | None = None,
               warmup: int = 100, replay_ts: int | None = None,
               live: bool = False) -> Session:
        if live:
            # pull recent klines as warmup context; the frontend takes over
            # via Binance WS streams for live updates
            tf_ms = TF_MS[tf]
            now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
            span_ms = max((warmup + 5) * tf_ms, 2 * 86_400_000)   # always span ≥ 2 days
            start_ms = now_ms - span_ms
            start_iso = dt.datetime.fromtimestamp(start_ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
            # _to_ms truncates to start-of-day, so end="today" would stop at last
            # midnight and miss all of today's klines. Pass tomorrow to capture
            # everything that has closed up to right now.
            end_iso = (dt.datetime.fromtimestamp(now_ms / 1000, dt.timezone.utc)
                       + dt.timedelta(days=1)).strftime("%Y-%m-%d")
            df = fetch_klines(symbol, tf, start_iso, end_iso, market=market)
            if df.empty:
                raise ValueError("no recent candles for live session")
            # drop the still-forming kline (if returned) so the chart's WS feed
            # owns it cleanly — avoids stale OHLC for the live bar
            tf_sec = tf_ms // 1000
            now_s = now_ms // 1000
            if int(df["time"].iloc[-1]) + tf_sec > now_s:
                df = df.iloc[:-1]
            df = df.tail(warmup + 5).reset_index(drop=True)
            if df.empty:
                raise ValueError("no recent candles for live session")
            cursor = len(df) - 1
        else:
            if start is None or end is None:
                raise ValueError("start and end are required for replay sessions")
            df = fetch_klines(symbol, tf, start, end, market=market)
            if df.empty:
                raise ValueError("no candles returned for that range")
            if replay_ts is not None:
                idx = int(df["time"].searchsorted(replay_ts, side="left"))
                cursor = max(0, min(idx, len(df) - 1))
            else:
                cursor = max(0, min(warmup, len(df) - 1))
        sid = secrets.token_hex(6)
        sess = Session(id=sid, symbol=symbol.upper(), market=market, tf=tf,
                       start=start or "", end=end or "",
                       df=df, cursor=cursor, is_live=live)
        with self._lock:
            self._sessions[sid] = sess
        return sess

    def get(self, sid: str) -> Session:
        with self._lock:
            if sid not in self._sessions:
                raise KeyError(sid)
            return self._sessions[sid]

    def delete(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

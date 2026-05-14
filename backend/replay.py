"""In-memory replay sessions: candles, cursor, trades, P&L."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from threading import RLock

import pandas as pd

from .binance import fetch_klines


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
    cursor: int                              # index of last revealed candle (0-based)
    trades: list[Trade] = field(default_factory=list)
    lock: RLock = field(default_factory=RLock)

    def candles_so_far(self) -> list[dict]:
        sub = self.df.iloc[: self.cursor + 1]
        return [
            {"time": int(r.time), "open": r.open, "high": r.high,
             "low": r.low, "close": r.close, "volume": r.volume}
            for r in sub.itertuples(index=False)
        ]

    def current_price(self) -> float:
        return float(self.df["close"].iloc[self.cursor])

    def current_time(self) -> int:
        return int(self.df["time"].iloc[self.cursor])

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

    def create(self, symbol: str, market: str, tf: str, start: str, end: str,
               warmup: int = 100, replay_ts: int | None = None) -> Session:
        df = fetch_klines(symbol, tf, start, end, market=market)
        if df.empty:
            raise ValueError("no candles returned for that range")
        if replay_ts is not None:
            # cursor = first candle whose open time is >= the requested replay timestamp
            idx = int(df["time"].searchsorted(replay_ts, side="left"))
            cursor = max(0, min(idx, len(df) - 1))
        else:
            cursor = max(0, min(warmup, len(df) - 1))
        sid = secrets.token_hex(6)
        sess = Session(id=sid, symbol=symbol.upper(), market=market, tf=tf,
                       start=start, end=end, df=df, cursor=cursor)
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

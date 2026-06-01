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
    # Symbol this trade belongs to. A session now spans multiple symbols (symbol
    # is a view), so trades are tagged and processed/shown per-symbol. Defaulted
    # for back-compat; tagged at placement and backfilled on snapshot load.
    symbol: str = ""

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
            "symbol": self.symbol,
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


def _trade_from_dict(d: dict) -> Trade | None:
    try:
        return Trade(
            id=str(d["id"]),
            symbol=str(d.get("symbol", "")),
            side=str(d["side"]),
            qty=float(d["qty"]),
            order_type=str(d.get("order_type", "market")),
            created_time=int(d["created_time"]),
            status=str(d.get("status", "pending")),
            limit_price=_opt_float(d.get("limit_price")),
            entry_time=_opt_int(d.get("entry_time")),
            entry_price=_opt_float(d.get("entry_price")),
            sl=_opt_float(d.get("sl")),
            tp=_opt_float(d.get("tp")),
            exit_time=_opt_int(d.get("exit_time")),
            exit_price=_opt_float(d.get("exit_price")),
            exit_reason=d.get("exit_reason"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _opt_float(v) -> float | None:
    return None if v is None else float(v)


def _opt_int(v) -> int | None:
    return None if v is None else int(v)


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
    user_id: int | None = None               # owning user; None only in legacy / single-user mode
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
    # opaque per-session UI state owned by the frontend (active indicators,
    # h-lines, measure, last-used lots/SL/TP). Persisted verbatim so a restored
    # session comes back with the same chart setup; the backend never reads it.
    client_state: dict = field(default_factory=dict)
    # per-symbol replay resume state so switching the symbol view (then back)
    # lands on the same bar: {symbol: {"start", "end", "cursor_time"}}.
    # Replay only — live is always pinned to the latest candle.
    symbol_views: dict = field(default_factory=dict)
    # cache of 1m klines per candle (keyed by candle open time), used to pin
    # the precise within-candle minute a replay limit/SL/TP filled. Not persisted.
    minute_cache: dict = field(default_factory=dict)

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

    def forming_ticks(self) -> list[dict]:
        """Revealed aggTrades of the current forming candle, in the same shape as
        step_tick's `new_ticks`. Lets the frontend rebuild its partial-candle
        buffer after a tick-level rewind (where no *new* ticks are streamed), so
        Previous redraws the shrunken partial instead of dropping it."""
        if self.tick_aggs is None or self.tick_idx == 0:
            return []
        revealed = self.tick_aggs.iloc[: self.tick_idx]
        return [
            {
                "time_ms": int(t.time_ms),
                "price": float(t.price),
                "qty": float(t.qty),
                "side": "sell" if bool(t.is_buyer_maker) else "buy",
            }
            for t in revealed.itertuples(index=False)
        ]

    def current_price(self) -> float:
        if self.tick_aggs is not None and self.tick_idx > 0:
            return float(self.tick_aggs.iloc[self.tick_idx - 1]["price"])
        return float(self.df["close"].iloc[self.cursor])

    def current_time(self) -> int:
        if self.tick_aggs is not None and self.tick_idx > 0:
            return int(self.tick_aggs.iloc[self.tick_idx - 1]["time_ms"] // 1000)
        return int(self.df["time"].iloc[self.cursor])

    def cursor_anchor_time(self) -> int:
        """The replay 'now', used to pin the cursor across tf/symbol switches and
        to time-stamp market entries / manual closes. Unlike current_time (the
        candle OPEN, which the tape needs), this is the END of the revealed
        candle: a market trade on a 4h 12–16 bar maps to the 15–16 bar on 1h,
        and dropping tf reveals through that last sub-candle. Tick/live keep the
        exact tick time."""
        if self.tick_aggs is not None and self.tick_idx > 0:
            return int(self.tick_aggs.iloc[self.tick_idx - 1]["time_ms"] // 1000)
        base = int(self.df["time"].iloc[self.cursor])
        if self.is_live:
            return base
        return base + TF_MS[self.tf] // 1000 - 1

    def _minute_klines(self, candle_open: int):
        """1m klines inside the candle opening at `candle_open`, cached. None
        when there's nothing finer to refine with (already 1m, or live)."""
        if self.is_live or self.tf == "1m":
            return None
        if candle_open in self.minute_cache:
            return self.minute_cache[candle_open]
        tf_s = TF_MS[self.tf] // 1000
        s = dt.datetime.fromtimestamp(candle_open, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        e = dt.datetime.fromtimestamp(candle_open + tf_s, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            m = fetch_klines(self.symbol, "1m", s, e, market=self.market)
        except Exception:
            m = None
        if m is not None and not m.empty:
            m = m[(m["time"] >= candle_open) & (m["time"] < candle_open + tf_s)].reset_index(drop=True)
        self.minute_cache[candle_open] = m if (m is not None and not m.empty) else None
        return self.minute_cache[candle_open]

    def _refine_time(self, candle_open: int, level: float, side: str, kind: str) -> int:
        """Precise unix-second the candle's price first crossed `level` (limit
        fill / SL / TP), found from 1m klines. Falls back to candle_open when no
        finer data is available — so outcomes are unchanged, only the timestamp
        is sharpened (mapping correctly on lower timeframes, like live)."""
        m = self._minute_klines(candle_open)
        if m is None:
            return candle_open
        long = side == "long"
        for r in m.itertuples(index=False):
            hi, lo = float(r.high), float(r.low)
            if kind == "tp":
                crossed = (hi >= level) if long else (lo <= level)
            else:                                   # limit fill or sl
                crossed = (lo <= level) if long else (hi >= level)
            if crossed:
                return int(r.time)
        return candle_open

    def modify_trade(self, tid: str, *, sl=None, tp=None,
                     limit_price=None, qty=None,
                     clear_sl=False, clear_tp=False) -> Trade:
        """Edit a still-active trade in place. Only non-None args change a field
        (so callers patch one level at a time, e.g. a single dragged SL line);
        clear_sl / clear_tp remove that level entirely. Mirrors the placement-time
        validation in main.place_trade: limit_price only while pending, SL/TP must
        sit on the correct side of the reference (limit for pending, entry for
        open). Raises KeyError if the trade isn't active, ValueError on an invalid
        level."""
        t = next((x for x in self.trades
                  if x.id == tid and x.status in ("open", "pending")), None)
        if t is None:
            raise KeyError(tid)

        new_qty = t.qty if qty is None else float(qty)
        if not (new_qty > 0):
            raise ValueError("qty must be > 0")

        new_limit = t.limit_price
        if limit_price is not None:
            if t.status != "pending":
                raise ValueError("limit_price can only change while order is pending")
            lp = float(limit_price)
            mark = self.current_price()
            if t.side == "long" and lp >= mark:
                raise ValueError("long limit must be below market price")
            if t.side == "short" and lp <= mark:
                raise ValueError("short limit must be above market price")
            new_limit = lp

        new_sl = None if clear_sl else (t.sl if sl is None else float(sl))
        new_tp = None if clear_tp else (t.tp if tp is None else float(tp))
        ref = new_limit if t.status == "pending" else t.entry_price
        if ref is not None:
            if t.side == "long":
                if new_sl is not None and new_sl >= ref:
                    raise ValueError("long SL must be below entry")
                if new_tp is not None and new_tp <= ref:
                    raise ValueError("long TP must be above entry")
            else:
                if new_sl is not None and new_sl <= ref:
                    raise ValueError("short SL must be above entry")
                if new_tp is not None and new_tp >= ref:
                    raise ValueError("short TP must be below entry")

        t.qty = new_qty
        t.limit_price = new_limit
        t.sl = new_sl
        t.tp = new_tp
        return t

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

    def rewind_trades(self, new_time: int) -> None:
        """Drop trades created after `new_time` and reopen any whose fill/exit
        happened after it. Shared by candle-level `back` and tick-level rewind."""
        survivors: list[Trade] = []
        for t in self.trades:
            if t.symbol and t.symbol != self.symbol:
                survivors.append(t)               # other symbols sit at their own cursor
                continue
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
        self.trades = survivors

    def step_back_tick(self, n: int) -> None:
        """Tick-level Previous: rewind n revealed aggTrades, crossing candle
        boundaries so it keeps stepping tick-by-tick into the *previous* candle
        rather than dropping to candle-level once the forming candle empties
        (the mirror of step_tick's forward auto-advance). Settles at candle
        level when the previous candle has no aggTrades (gap) or we hit the
        start. Reverts trades to the new cursor time, mirroring `back`."""
        remaining = n
        while remaining > 0:
            # rewind within the current forming candle
            if self.tick_aggs is not None and self.tick_idx > 0:
                take = min(remaining, self.tick_idx)
                self.tick_idx -= take
                remaining -= take
                if remaining == 0:
                    break
            # forming candle exhausted (tick_idx == 0) — cross into the previous
            # candle and reveal it fully, then the loop rewinds into its ticks
            if self.cursor <= 0:
                self.reset_tick_state()
                break
            self.cursor -= 1
            self._load_tick_aggs()              # aggs for new forming candle df[cursor+1]
            if self.tick_aggs is None or self.tick_aggs.empty:
                self.reset_tick_state()         # no ticks for this candle — candle level
                break
            self.tick_idx = len(self.tick_aggs)
        self.rewind_trades(self.cursor_anchor_time())

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
            if t.symbol and t.symbol != self.symbol:
                continue
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
            if t.symbol and t.symbol != self.symbol:
                continue
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

    def to_snapshot(self) -> dict:
        """Serialize the parts of the session worth persisting. The kline df,
        cvd cache, tick aggs, and live aggtrade buffer are all regenerable; we
        only keep cursor + trades + identity + mode flags."""
        return {
            "v": 1,
            "id": self.id,
            "user_id": self.user_id,
            "symbol": self.symbol,
            "market": self.market,
            "tf": self.tf,
            "start": self.start,
            "end": self.end,
            "cursor": int(self.cursor),
            # the cursor index can go stale vs a re-fetched df (extend_history
            # prepends bars without widening start); persist its time so a cold
            # hydrate can re-derive the index instead of indexing out of bounds.
            # Anchor on the candle END so a finer-tf hydrate reveals through it.
            "cursor_time": self.cursor_anchor_time() if len(self.df) else None,
            "is_live": bool(self.is_live),
            "trades": [t.to_dict() for t in self.trades],
            "client_state": self.client_state,
            "symbol_views": self.symbol_views,
        }

    def apply_snapshot(self, snap: dict) -> None:
        """Restore mutable state from a snapshot. Caller is responsible for
        rebuilding self.df (e.g. by re-fetching klines for the same range)
        before calling this."""
        self.cursor = int(snap.get("cursor", self.cursor))
        if "is_live" in snap:
            self.is_live = bool(snap["is_live"])
        rebuilt = []
        for d in snap.get("trades") or []:
            t = _trade_from_dict(d)
            if t is not None:
                rebuilt.append(t)
        # Legacy snapshots predate per-trade symbol tags — tag them with the
        # session's symbol so they keep showing/processing under it.
        for t in rebuilt:
            if not t.symbol:
                t.symbol = snap.get("symbol", self.symbol)
        self.trades = rebuilt
        self.client_state = snap.get("client_state") or {}
        self.symbol_views = snap.get("symbol_views") or {}
        self.reset_tick_state()

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
            if t.symbol and t.symbol != self.symbol:
                continue                          # only the symbol in view advances
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
                t.entry_time = self._refine_time(rt, t.limit_price, t.side, "limit")
                t.entry_price = entry
                just_filled.add(t.id)
                changed.append(t)

        # 2) SL/TP on open trades — only when SL is set, and never on the same
        # candle that filled the trade.
        for t in self.trades:
            if t.symbol and t.symbol != self.symbol:
                continue
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
                level = t.sl if reason == "sl" else t.tp
                t.exit_time = self._refine_time(rt, level, t.side, reason)
                t.exit_price = hit_price
                t.exit_reason = reason
                changed.append(t)

        return changed


def _live_window_df(symbol: str, market: str, tf: str, warmup: int):
    """Recent klines as warmup context for a live session; the frontend takes
    over via Binance WS streams for live updates. Returns (df, cursor)."""
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
    return df, len(df) - 1


def _replay_cursor(df, replay_ts: int | None, warmup: int) -> int:
    """Index of the bar to park the cursor on for a replay session."""
    if replay_ts is not None:
        idx = int(df["time"].searchsorted(replay_ts, side="left"))
        return max(0, min(idx, len(df) - 1))
    return max(0, min(warmup, len(df) - 1))


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = RLock()

    def create(self, symbol: str, market: str, tf: str,
               start: str | None = None, end: str | None = None,
               warmup: int = 100, replay_ts: int | None = None,
               live: bool = False,
               inherit_trades: list[dict] | None = None,
               user_id: int | None = None,
               sid: str | None = None) -> Session:
        symbol = symbol.upper()
        if live:
            df, cursor = _live_window_df(symbol, market, tf, warmup)
        else:
            if start is None or end is None:
                raise ValueError("start and end are required for replay sessions")
            df = fetch_klines(symbol, tf, start, end, market=market)
            if df.empty:
                raise ValueError("no candles returned for that range")
            cursor = _replay_cursor(df, replay_ts, warmup)
        sid = sid or secrets.token_hex(6)
        sess = Session(id=sid, symbol=symbol, market=market, tf=tf,
                       start=start or "", end=end or "",
                       df=df, cursor=cursor, is_live=live, user_id=user_id)
        if inherit_trades:
            rebuilt = (_trade_from_dict(t) for t in inherit_trades)
            sess.trades = [t for t in rebuilt if t is not None]
            for t in sess.trades:
                if not t.symbol:
                    t.symbol = symbol
        if not live:
            sess.symbol_views[symbol] = {
                "start": start or "", "end": end or "",
                "cursor_time": int(df["time"].iloc[cursor]),
            }
        with self._lock:
            self._sessions[sid] = sess
        return sess

    def set_view(self, sess: Session, symbol: str, tf: str, *,
                 start: str | None = None, end: str | None = None,
                 replay_ts: int | None = None, warmup: int = 100,
                 fresh: bool = False) -> Session:
        """Re-point a session to a new (symbol, tf) view, keeping all trades /
        client_state / id / user_id. Replay resumes each symbol at the bar it
        was last left on (via symbol_views); a brand-new symbol uses the
        request's start/end/replay_ts. Live always pins to the latest candle.

        fresh=True ignores any saved resume point and anchors to the request's
        date (used by a date-jump reset on the current symbol)."""
        symbol = symbol.upper()
        with sess.lock:
            if not sess.is_live and not fresh:
                # remember where we're leaving the outgoing symbol
                sess.symbol_views[sess.symbol] = {
                    "start": sess.start, "end": sess.end,
                    "cursor_time": sess.cursor_anchor_time(),
                }
            if sess.is_live:
                df, cursor = _live_window_df(symbol, sess.market, tf, warmup)
                new_start = new_end = ""
            else:
                saved = None if fresh else sess.symbol_views.get(symbol)
                if saved and saved.get("start") and saved.get("end"):
                    new_start, new_end = saved["start"], saved["end"]
                    df = fetch_klines(symbol, tf, new_start, new_end, market=sess.market)
                    if df.empty:
                        raise ValueError("no candles returned for that range")
                    # map the anchor time to the candle that CONTAINS it
                    # (largest open <= anchor), so a 4h end-time lands on the
                    # right finer bar rather than overshooting to the next.
                    idx = int(df["time"].searchsorted(int(saved.get("cursor_time", 0)), side="right")) - 1
                    cursor = max(0, min(idx, len(df) - 1))
                else:
                    if start is None or end is None:
                        raise ValueError("start and end are required for replay sessions")
                    new_start, new_end = start, end
                    df = fetch_klines(symbol, tf, start, end, market=sess.market)
                    if df.empty:
                        raise ValueError("no candles returned for that range")
                    cursor = _replay_cursor(df, replay_ts, warmup)
            sess.symbol = symbol
            sess.tf = tf
            sess.df = df
            sess.cursor = cursor
            sess.start = new_start
            sess.end = new_end
            sess.reset_tick_state()
            sess.cvd_cache.clear()
            sess.live_aggtrades.clear()
            if not sess.is_live:
                sess.symbol_views[symbol] = {
                    "start": new_start, "end": new_end,
                    "cursor_time": int(df["time"].iloc[cursor]),
                }
        return sess

    def get(self, sid: str) -> Session:
        with self._lock:
            if sid not in self._sessions:
                raise KeyError(sid)
            return self._sessions[sid]

    def delete(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

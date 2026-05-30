"""Unit tests for the Trade P&L and Session candle-processing logic in replay.py.

These build a Session directly with a tiny one-candle DataFrame — no network,
no SessionStore — and exercise process_candle (limit fills + SL/TP) plus the
snapshot round-trip.
"""
import pandas as pd

from backend.replay import Session, Trade


def make_session(o, h, l, c, trades=None):
    df = pd.DataFrame(
        [{"time": 1_000, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}]
    )
    s = Session(id="t", symbol="X", market="spot", tf="1h",
                start="", end="", df=df, cursor=0)
    s.trades = trades or []
    return s


def open_trade(side, entry, sl=None, tp=None, tid="1"):
    return Trade(id=tid, side=side, qty=2.0, order_type="market", created_time=0,
                 status="open", entry_time=0, entry_price=entry, sl=sl, tp=tp)


# --- Trade.pnl -------------------------------------------------------------

def test_pnl_long_open_uses_mark():
    assert open_trade("long", 100.0).pnl(110.0) == 20.0


def test_pnl_short_open_inverts():
    assert open_trade("short", 100.0).pnl(110.0) == -20.0


def test_pnl_closed_uses_exit_price_not_mark():
    t = open_trade("long", 100.0)
    t.status = "closed"
    t.exit_price = 105.0
    assert t.pnl(999.0) == 10.0   # mark ignored once closed


def test_pnl_pending_is_zero():
    t = Trade(id="1", side="long", qty=2.0, order_type="limit",
              created_time=0, status="pending", limit_price=100.0)
    assert t.pnl(120.0) == 0.0


# --- process_candle: limit fills ------------------------------------------

def test_limit_long_fills_on_intra_candle_touch():
    t = Trade(id="1", side="long", qty=1.0, order_type="limit",
              created_time=0, status="pending", limit_price=100.0)
    s = make_session(101, 102, 99, 100.5, [t])   # low dips to 99, touches 100
    s.process_candle()
    assert t.status == "open" and t.entry_price == 100.0


def test_limit_long_gap_down_fills_at_open():
    t = Trade(id="1", side="long", qty=1.0, order_type="limit",
              created_time=0, status="pending", limit_price=100.0)
    s = make_session(98, 99, 97, 98.5, [t])       # opened below the limit
    s.process_candle()
    assert t.status == "open" and t.entry_price == 98.0


# --- process_candle: SL / TP ----------------------------------------------

def test_long_sl_hit_closes_at_sl():
    t = open_trade("long", 100.0, sl=90.0)
    s = make_session(95, 96, 85, 88, [t])         # low pierces SL
    s.process_candle()
    assert t.status == "closed" and t.exit_price == 90.0 and t.exit_reason == "sl"


def test_long_tp_hit_closes_at_tp():
    t = open_trade("long", 100.0, sl=90.0, tp=110.0)
    s = make_session(101, 112, 100, 111, [t])     # high pierces TP
    s.process_candle()
    assert t.status == "closed" and t.exit_price == 110.0 and t.exit_reason == "tp"


def test_short_sl_hit_closes_at_sl():
    t = open_trade("short", 100.0, sl=110.0)
    s = make_session(105, 115, 104, 112, [t])     # high pierces short SL
    s.process_candle()
    assert t.status == "closed" and t.exit_price == 110.0 and t.exit_reason == "sl"


def test_no_sl_no_exit():
    t = open_trade("long", 100.0, sl=None, tp=None)
    s = make_session(95, 96, 50, 60, [t])         # crashes, but no SL set
    s.process_candle()
    assert t.status == "open"                     # stays open by design


def test_same_candle_fill_is_not_sl_eligible():
    # pending limit that fills AND would breach SL on the same candle must not exit
    t = Trade(id="1", side="long", qty=1.0, order_type="limit",
              created_time=0, status="pending", limit_price=100.0, sl=90.0)
    s = make_session(100, 101, 80, 85, [t])       # fills at open 100, low 80 < SL 90
    s.process_candle()
    assert t.status == "open" and t.exit_reason is None


# --- step_back_tick: tick-level Previous ----------------------------------

def _two_candle_session():
    df = pd.DataFrame([
        {"time": 1_000, "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1.0},
        {"time": 4_600, "open": 100, "high": 110, "low": 100, "close": 105, "volume": 1.0},
    ])
    return Session(id="t", symbol="X", market="spot", tf="1h",
                   start="", end="", df=df, cursor=0)


def test_step_back_tick_rewinds_idx_and_reopens_trade():
    s = _two_candle_session()
    # forming candle (index 1) with three revealed ticks at t=1000,1001,1002s
    s.tick_aggs = pd.DataFrame([
        {"time_ms": 1_000_000, "price": 100.0, "qty": 1.0, "is_buyer_maker": False},
        {"time_ms": 1_001_000, "price": 101.0, "qty": 1.0, "is_buyer_maker": False},
        {"time_ms": 1_002_000, "price": 102.0, "qty": 1.0, "is_buyer_maker": False},
    ])
    s.tick_idx = 3
    t = open_trade("long", 102.0, tid="x")
    t.entry_time = 1_002                       # filled on the last tick
    s.trades = [t]

    s.step_back_tick(2)                        # rewind to idx 1 (cursor time 1000)

    assert s.tick_idx == 1
    assert t.status == "pending" and t.entry_time is None   # entry undone


def test_forming_ticks_returns_revealed_prints_for_partial_candle():
    s = _two_candle_session()
    s.tick_aggs = pd.DataFrame([
        {"time_ms": 1_000_000, "price": 100.0, "qty": 1.0, "is_buyer_maker": False},
        {"time_ms": 1_001_000, "price": 101.0, "qty": 2.0, "is_buyer_maker": True},
        {"time_ms": 1_002_000, "price": 102.0, "qty": 3.0, "is_buyer_maker": False},
    ])
    s.tick_idx = 2                                  # only first two revealed

    ticks = s.forming_ticks()

    assert [t["price"] for t in ticks] == [100.0, 101.0]
    assert [t["side"] for t in ticks] == ["buy", "sell"]   # is_buyer_maker -> sell
    assert ticks[1]["time_ms"] == 1_001_000 and ticks[1]["qty"] == 2.0


def test_forming_ticks_empty_when_not_mid_candle():
    s = _two_candle_session()
    assert s.forming_ticks() == []                  # tick_idx == 0, no aggs


def test_step_back_tick_falls_back_to_candle_when_no_ticks_revealed(monkeypatch):
    s = _two_candle_session()
    s.cursor = 1                               # fully revealed, no forming ticks
    s.tick_aggs = None
    s.tick_idx = 0
    # crossing into the previous candle tries to load its aggTrades; stub it out
    # to an empty frame so the rewind settles at candle level without network.
    monkeypatch.setattr("backend.replay.fetch_agg_trades",
                        lambda *a, **k: pd.DataFrame(
                            columns=["time_ms", "price", "qty", "is_buyer_maker"]))

    s.step_back_tick(5)

    assert s.cursor == 0 and s.tick_aggs is None


def _three_candle_session():
    df = pd.DataFrame([
        {"time": 1_000, "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1.0},
        {"time": 4_600, "open": 100, "high": 110, "low": 100, "close": 105, "volume": 1.0},
        {"time": 8_200, "open": 105, "high": 115, "low": 105, "close": 110, "volume": 1.0},
    ])
    return Session(id="t", symbol="X", market="spot", tf="1h",
                   start="", end="", df=df, cursor=0)


def test_step_back_tick_crosses_into_previous_candle_ticks(monkeypatch):
    # forming candle (df index 2) with 3 of its ticks revealed; rewinding 5 should
    # consume those 3, cross the boundary, and reveal the previous candle's aggs
    # tick-by-tick (not jump a whole candle to candle-level).
    s = _three_candle_session()
    s.cursor = 1                               # forming candle is df index 2
    s.tick_aggs = pd.DataFrame([
        {"time_ms": 8_200_000, "price": 105.0, "qty": 1.0, "is_buyer_maker": False},
        {"time_ms": 8_201_000, "price": 106.0, "qty": 1.0, "is_buyer_maker": False},
        {"time_ms": 8_202_000, "price": 107.0, "qty": 1.0, "is_buyer_maker": False},
    ])
    s.tick_idx = 3
    # the previous candle (df index 1) has 4 aggTrades
    prev = pd.DataFrame([
        {"time_ms": 4_600_000, "price": 100.0, "qty": 1.0, "is_buyer_maker": False},
        {"time_ms": 4_601_000, "price": 101.0, "qty": 1.0, "is_buyer_maker": False},
        {"time_ms": 4_602_000, "price": 102.0, "qty": 1.0, "is_buyer_maker": False},
        {"time_ms": 4_603_000, "price": 103.0, "qty": 1.0, "is_buyer_maker": False},
    ])
    monkeypatch.setattr("backend.replay.fetch_agg_trades", lambda *a, **k: prev)

    s.step_back_tick(5)                         # 3 to empty forming + 2 into prev

    # crossed back one candle, now forming the previous candle with 2 ticks left
    assert s.cursor == 0
    assert s.tick_idx == 2                      # 4 revealed then rewound by 2
    assert s.current_price() == 101.0           # price of the 2nd revealed tick


# --- snapshot round-trip ---------------------------------------------------

def test_snapshot_round_trip_preserves_trades_and_cursor():
    src = make_session(100, 110, 90, 105,
                       [open_trade("short", 100.0, sl=110.0, tp=95.0, tid="abc")])
    snap = src.to_snapshot()

    dst = make_session(100, 110, 90, 105)         # fresh, empty trades
    dst.cursor = 99                               # will be overwritten by snapshot
    dst.apply_snapshot(snap)

    assert dst.cursor == src.cursor
    assert len(dst.trades) == 1
    r = dst.trades[0]
    assert (r.id, r.side, r.qty, r.entry_price, r.sl, r.tp) == \
           ("abc", "short", 2.0, 100.0, 110.0, 95.0)

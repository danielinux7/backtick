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

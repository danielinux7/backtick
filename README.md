# Chart Replay

Local web app for replaying Binance candlestick history bar-by-bar and placing hypothetical trades for discretionary backtesting.

## Features
- Step / play-pause / rewind through candles (configurable step size and speed)
- Market and **limit** orders with optional SL/TP entered as % from entry
- Click on the chart to set limit / SL / TP prices (auto-converted to %)
- Trades without an SL stay open until you close them manually (no TP auto-exit either)
- Timeframe per session: 1m / 5m / 15m / 30m / 1h / 4h / 1d
- Indicators: EMA 20/50/200, RSI 14 (computed in JS, doesn't slow replay)
- Drawing tools: horizontal price lines, two-click measure with Δprice / %Δ / time / bars
- Trade log with open & closed P&L, win/loss count, win rate
- Disk cache (parquet) with gap-fill so re-fetching old ranges is fast and never partial

## Setup
```bash
python3 -m venv /tmp/chart_replay_venv
/tmp/chart_replay_venv/bin/pip install -r requirements.txt
```

## Run
```bash
/tmp/chart_replay_venv/bin/uvicorn backend.main:app --reload --port 8765 \
  --app-dir /home/danial/Documents/life/trading/chart_replay_app
```
Open <http://localhost:8765>. The page auto-loads SOLUSDT 4h with replay date 60 days back.

## Workflow
1. Pick a symbol, market (spot/futures), timeframe, replay date (dd/mm/yyyy), and warmup-candle count, then click **Load**.
2. Warmup candles appear up to and including the candle whose open time equals your replay date — the cursor sits on that candle, and stepping reveals what came after.
3. Use Next / Play / `←` / `→` to advance. SL/TP are checked against each new candle's high/low.
4. Place trades via the side panel, or `L` / `S` shortcuts. Click `close` / `cancel` in the trades table to exit or cancel a pending limit.

## Chart tools
- **🎯 next to a field** (limit price / SL / TP): click the chart at the level you want — for SL/TP this is converted back to a % from entry, with side validated against the "Side for picks" selector.
- **H-line**: drop a horizontal price line at the next chart click; stays armed so you can drop several. Hotkey `H`.
- **Measure**: click two points on the chart; result shows Δprice, %Δ, time span, and bar count in the toolbar. Hotkey `M`.
- **Clear**: removes all H-lines and measurements. `Esc` cancels any armed tool.

## Keyboard
- `→` step forward, `←` step back
- `space` toggle play/pause
- `L` long, `S` short
- `H` horizontal line, `M` measure
- `Esc` cancel current tool / pick

## Layout
```
backend/
  main.py        FastAPI app + routes
  binance.py     historical klines + parquet cache + gap-fill
  replay.py      Session / Trade / SessionStore
frontend/
  index.html
  app.js         lightweight-charts wiring, JS-side indicators, tools
  style.css
data_cache/      parquet cache (gitignored)
```

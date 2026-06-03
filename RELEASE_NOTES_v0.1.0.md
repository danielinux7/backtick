# Backtick v0.1.0 — first public release

Backtick is a local-first, open-source web app for replaying Binance history
candle-by-candle and tick-by-tick, with real order-flow tooling and hypothetical
trades — built for discretionary backtesting.

## Highlights

- **True time & sales** — every print tagged buy/sell from Binance aggTrades'
  real `is_buyer_maker` flag, with a large-prints percentile highlighter.
- **Tick-by-tick replay + synced tape** — step or auto-play individual aggTrades
  as the forming candle rebuilds, tape in lockstep; 1×–200×.
- **Footprint** — per-candle buy/sell volume by price level.
- **Hypothetical trades** — market/limit orders with SL/TP (as % or chart-picked),
  resolved candle- and tick-level against real price action.
- **Indicators & tools** — EMA, RSI, CVD, Volume, Volume Profile, Liquidations;
  horizontal-line and measure drawing tools; watchlist.
- **Live + replay modes**, Google/email sign-in, deterministic account avatars.
- **Installable PWA**, offline shell, mobile-friendly.
- **Public site** — marketing landing at `/`, the app at `/app`, plus Docs,
  About, and Contact pages.
- **Open source** — MIT licensed; runs locally or self-hosted; your trades stay
  in your own session.

## Stack

FastAPI backend (Binance klines + aggTrades, parquet disk cache with gap-fill),
vanilla-JS front end on TradingView lightweight-charts. Deploys to Render.

---

_Not financial advice. Markets are risky._

<!-- Release commands (run by maintainer when ready):
git tag -a v0.1.0 -m "Backtick v0.1.0 — first public release"
git push origin v0.1.0
gh release create v0.1.0 --title "v0.1.0 — first public release" --notes-file RELEASE_NOTES_v0.1.0.md
gh repo edit --visibility public
-->

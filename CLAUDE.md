# Backtick — Chart Replay

Local-first web app for replaying Binance candlestick history bar-by-bar and placing
hypothetical trades for **discretionary backtesting**. FastAPI backend + vanilla-JS
lightweight-charts PWA frontend. Deployed on Render (`render.yaml`, auto-deploy on push).

## File map

**Backend (`backend/`)**
- `main.py` — FastAPI app + all chart/replay routes, `/healthz`.
- `binance.py` — historical klines via Binance REST + parquet disk cache with gap-fill (`fetch_klines`, pure `_missing_ranges`).
- `replay.py` — core domain: `Session` / `Trade` / `SessionStore`. SL/TP + limit-fill logic in `process_candle` (candle-level) and `_process_tick` (tick-level). Persist/restore via `to_snapshot` / `apply_snapshot`.
- `aggtrades.py` — aggTrades fetch for tick replay / CVD.
- `exchange_info.py`, `snapshots.py`, `db.py`, `models.py` — symbols, session persistence, DB.
- `auth.py`, `routes_auth.py`, `routes_symbols.py` — Google OAuth + email auth, watchlist.

**Frontend (`frontend/`)**
- `index.html` — layout (setup form, chart panes, trade side-pane, trades table `#trades-table`).
- `app.js` — chart wiring, JS-side indicators (EMA/RSI/CVD), drawing tools, replay controls.
- `sw.js` — service worker; **`CACHE_VERSION` must bump on every deploy** (see below).
- `manifest.webmanifest`, `pwa.js`, `auth-modal.js`, `symbol-picker.js`.

**Scripts**
- `scripts/bump_sw_version.py` — auto-bumps `sw.js` `CACHE_VERSION`; runs in Render `buildCommand`.

## Run

Per venv policy (never install to system Python):
```bash
python3 -m venv /tmp/backtick_venv          # or reuse ~/.venvs/backtick
/tmp/backtick_venv/bin/pip install -r requirements.txt -r requirements-dev.txt
/tmp/backtick_venv/bin/uvicorn backend.main:app --reload --port 8765
```
Open <http://localhost:8765>. Page auto-loads SOLUSDT 4h ~60 days back.

## Test

```bash
/tmp/backtick_venv/bin/pytest -q                  # backend unit tests (no network)
npx playwright test                               # frontend E2E (needs server on :8765)
```
- Unit tests cover the riskiest logic: gap-fill (`_missing_ranges`) and trade SL/TP/limit fills + snapshot round-trip.
- Playwright smoke test loads a symbol, steps a candle, places a long, asserts it appears in the trades table. It hits live Binance, so treat a network failure as flaky, not a regression.

## Workflow (plan → execute with testing)

1. **Plan first.** Enter plan mode (`Shift+Tab` to cycle, or your bound key) before non-trivial work.
2. **Reference specifics when prompting** — name the exact file/function (e.g. "the SL gap-fill in `process_candle`"), state constraints up front, and point to an example pattern to mirror.
3. **Execute with tests in mind** — add/adjust a test alongside the change; run `pytest -q` before committing.

## Reference patterns (mirror these)

- **Pure, testable helpers** sit beside their callers — e.g. `_missing_ranges` in `binance.py`. Keep new logic pure where possible.
- **Trade (de)serialization** goes through `Trade.to_dict` ⇄ `_trade_from_dict`; session persistence through `to_snapshot` ⇄ `apply_snapshot`. Add new persisted fields in all four.
- **`_opt_float` / `_opt_int`** for nullable numeric parsing from dicts.

## Constraints

- **Commits go on a `dev/<topic>` branch, never `main`.** Check `git branch --show-current` first.
- **venv only** — never `pip install` into system Python (PEP 668 / `--break-system-packages` forbidden).
- **Do not "fix" the risk model.** Wide stop-loss / small take-profit is intentional (discretionary "spot-with-margin" strategy). Math reality-checks are welcome; "your RR is bad" redesigns are not.
- **Bump the SW cache** on any deploy that changes frontend assets — `scripts/bump_sw_version.py` does this in the Render build, but verify `CACHE_VERSION` changed or clients serve stale files.
- Secrets (`SESSION_SECRET`, Google OAuth, any bot tokens) live in env vars — never commit them.

## Using Claude effectively — one-time manual setup

These can't be committed (they're account/machine actions). Do them once:

- **Drive Claude from your phone — Remote Control.** Keep Claude Code running on this machine; install the Claude mobile app and enable Remote Control to steer the session and get push notifications when it needs input. (Replaces a Slack/Telegram bot — code never leaves your machine.)
- **Claude in Chrome** — install the extension for interactive UI checks against the live PWA (complements the automated Playwright MCP wired in `.mcp.json`).
- **Code-intelligence (LSP) plugin** — in Claude Code run `/plugin`, add a marketplace, and install a Python LSP plugin so Claude gets go-to-def / find-refs / live diagnostics on edits.

# NQ Futures Trading Bot - API Contract & Architecture

## Overview

This document describes the actual architecture of the NQ Futures Trading Bot and the contract between the FastAPI backend and the frontend dashboard. The backend is a FastAPI application (`backend/api.py`) that runs a paper-trading momentum bot against the NQ (E-mini Nasdaq-100) / MNQ (Micro E-mini) futures.

The system is **PAPER mode only** by default. Live trading is blocked unless explicitly authorized (see Risk Manager below).

---

## 1. Architecture

The application is a single FastAPI process serving both the REST/WebSocket API and the SPA dashboard (`frontend/index.html`). There is no separate trading engine; the strategy, risk manager, auto-trader, and paper-trader all run in-process. Key components:

| Component | File | Role |
|-----------|------|------|
| API server | `backend/api.py` | FastAPI app, endpoints, webhook, dashboard payload, WebSocket |
| Auth | `backend/auth.py` | `X-API-Key` / `ADMIN_API_KEY` enforcement |
| Risk Manager | `backend/risk_manager.py` | Eval limits, buffer zones, kill switch, persistence |
| Strategy | `backend/strategy.py` | NQ momentum signal generation (SMA + RSI + momentum score) |
| Auto-Trader | `backend/auto_trader.py` | Background loop: fetches NQ=F data, feeds strategy, executes paper trades |
| Paper Trader | `backend/paper_trader.py` | Simulated account equity / positions / P&L (source of truth) |
| Data provider | `backend/data_provider.py` | Yahoo Finance `NQ=F` hourly/daily feed, synthetic fallback |
| IBKR client | `backend/ibkr_client.py` | `ib_insync` wrapper; connect is bypassed inside the FastAPI loop (paper mode) |
| Databento client | `backend/databento_client.py` | Optional historical client; sample-data fallback when no key |
| Rollover | `backend/rollover.py` | Front-month contract expiry (third Friday, quarterly) |
| Backtest | `backend/backtest_runner.py`, `backend/backtest_engine.py` | Walk-forward validation over synthetic seeded windows |
| Reconciliation | `backend/reconciliation.py` | Paper-state vs broker position diff |
| Database | `backend/database.py` | SQLite (`nq_bot.db`) tables incl. `risk_state` |

### Mode

The bot runs in `PAPER` mode (`bot_state["mode"]` is `"PAPER"`). A `LIVE` start is rejected (HTTP 403) unless both `allow_live_trading: true` is set in `config/risk_config.yaml` **and** the `ALLOW_LIVE_TRADING` env var is set (`backend/api.py:510-517`).

### Data feed

The auto-trader fetches `NQ=F` hourly bars from Yahoo Finance via `yfinance` and feeds them through `strategy.on_tick()`. Databento is optional; a valid `DATABENTO_API_KEY` merely enables a historical client, otherwise sample data is returned. IBKR connect is skipped inside the FastAPI event loop, so order placement returns a simulated ID — no live broker order flow.

---

## 2. Environment & Configuration

- **Default mode:** Paper (`TRADING_MODE=PAPER`).
- **Secrets** (`ADMIN_API_KEY`, `WEBHOOK_SECRET`) live in `.env` (generated automatically if not set). See `config/.env.example` for the template (`TRADING_MODE`, `IBKR_*`, `DATABENTO_API_KEY`, `API_HOST`, `API_PORT`).
- `config/risk_config.yaml` defines the eval limits (see Risk Controls below).

---

## 3. API Endpoints

Base URL: `http://localhost:8888/api/v1` (port 8888 default; port 80 when run as root for TradingView webhooks).

Auth conventions:
- **Control / backtest / reconciliation / signal-list endpoints** require the `X-API-Key` header (admin key from `ADMIN_API_KEY`).
- **TradingView webhook** authenticates with the shared secret via `?token=` query param or a `token` field in the JSON body (TradingView cannot send custom headers).
- **WebSocket** requires the token for non-loopback clients; tokenless loopback is allowed.
- Missing headers on protected endpoints return HTTP 422; wrong key returns HTTP 401. Invalid body returns HTTP 400/413.

### Read endpoints (no auth)

#### `GET /status`
Returns bot execution status and health.

Response (`BotStatus`, `shared/schema.py`):
```json
{"status":"RUNNING","mode":"PAPER","timestamp":"2026-08-14T12:00:00",
 "ibkr_connected":false,"databento_connected":false,
 "active_contract":"NQU6","days_to_expiration":18,"rollover_pending":false}
```

#### `GET /positions`
Returns the paper account summary and open positions.

Response (`AccountPositions`):
```json
{"account_id":"DU_PAPER_NQ_MNQ","net_liquidation":50000.0,"margin_used":0.0,
 "margin_available":50000.0,
 "positions":[{"symbol":"NQ","quantity":1,"avg_px":18500.0,"unrealized_pnl":0.0,"realized_pnl":0.0}]}
```

#### `GET /pnl`
Returns P&L summary. `paper_state` is the live source of truth, with the DB `pnl_records` table as a restart backup.

Response (`PnLSummary`):
```json
{"daily_pnl":570.0,"total_pnl":3420.0,"max_drawdown":-1250.0,"win_rate":0.58}
```

#### `GET /trades`
Returns the last 50 trade-log rows from the `trades` table.

Response (`TradeLogResponse`):
```json
{"trades":[{"trade_id":"auto_ab12cd34ef56","timestamp":"2026-08-14T09:30:15",
  "symbol":"NQ","side":"BUY","quantity":1,"price":18545.0,
  "reason":"Momentum score 0.71 > 0.65","order_type":"MARKET"}]}
```

#### `GET /dashboard`
Single-call payload for the dashboard (also what the WebSocket broadcasts). Includes `status`, `account`, `pnl`, `dll`, `buffer`, `consistency`, `trades_today`, `profit_target`, and `auto_trade` keys.

#### `GET /autotrade/status`
Returns auto-trader state: `auto_trading`, `stopped_reason`, `equity`, `hard_stop`, `profit_target`, `bars_fed`, `trades_executed`, `position_qty`, `position_side`, `last_price`.

#### `GET /webhook/signals` *(requires `X-API-Key`)*
Returns the last 50 TradingView signals (`TradingViewSignalListResponse`).

### Authenticated control endpoints (require `X-API-Key`)

| Method & Path | Body | Description |
|---------------|------|-------------|
| `POST /control/start` | `{"mode":"PAPER"}` | Start the bot. `LIVE` rejected 403 unless config + env authorize it. |
| `POST /control/stop` | `{"flatten":true}` | Stop the bot; flatten positions if requested. |
| `POST /paper/reset` | — | Reset paper account to $50,000; clears P&L, positions, risk state, DB records. |
| `POST /autotrade/start` | — | Start the background auto-trader loop. |
| `POST /autotrade/stop` | — | Stop the background auto-trader loop. |
| `GET /reconciliation` | — | Diff intended (`paper_state`) vs broker positions. |
| `GET /backtest/results` | `?seed=42` | Walk-forward validation result (`BacktestResultResponse`). |
| `GET /backtest/single` | `?seed=42` | Single full-period backtest (`SingleBacktestResultResponse`). |
| `POST /webhook/test` | `TradingViewTestRequest` | Simulate a TradingView signal through the same risk/execution path. |

### TradingView webhook (secret-token auth)

#### `POST /webhook/tradingview?token=...`
Accept plain-text (`"BUY NQ 2 @18500"`, `"SELL MNQ"`, `"FLATTEN"`) or JSON (`{"action":"BUY","symbol":"NQ","quantity":2,"price":18500,"token":"..."}`) alerts.

- Verifies the token; missing/wrong token returns 401.
- Enforces an 8 KB payload limit (413) and a rate limit of 10 signals / 60 s per IP (429).
- Runs the signal through the same risk checks as manual orders and records it in the `tv_signals` table (payload token redacted).
- Returns `{"status": "EXECUTED" | "REJECTED" | "BLOCKED", ...}` with `signal_id`, `trade_id`, and protective stops on success.

---

## 4. WebSocket (`/ws`)

Pushes the full dashboard payload (same shape as `GET /dashboard`) to all connected clients every 2 seconds. Auth: tokenless loopback clients are accepted; non-loopback must present a valid `WS_TOKEN` / `WEBHOOK_SECRET` token via the `?token=` query param (else 1008 close). Sending `"ping"` returns `{"type":"pong"}`.

---

## 5. Contract Rollover

`ContractRolloverManager` (`backend/rollover.py`) computes the front-month contract from CME quarterly expirations (third Friday of Mar/Jun/Sep/Dec) and reports `active_contract` (e.g. `NQU6`), `days_to_expiration`, and `rollover_pending` (true within the 5-day threshold). The strategy flattens when a rollover is pending and a position is open.

---

## 6. Risk Controls

From `config/risk_config.yaml` / `backend/risk_manager.py`:

| Rule | Value | Behavior |
|------|-------|----------|
| Profit target | $3,000 | Reaching this passes the eval goal. |
| Daily Loss Limit (DLL) | $1,200 | SOFT breach — pauses trading for the day (resets next day). |
| Max Loss Limit (MLL) / trailing drawdown | $2,000 | HARD breach — trailing drawdown from peak equity, permanently locks the account. |
| Max contracts | 4 minis / 40 micros | Hard cap per symbol. |
| Buffer zones | 50% warning / 75% critical / 90% danger | Position scaling: warning = 2 minis, critical = 1 mini, danger = 0 (locked). |
| Max trades / day | 6 | Daily trade-count cap. |
| Max risk / trade | $500 | Worst-case USD risk at buffer-scaled stop. |
| Max consecutive losses | 3 | Triggers cooldown. |
| Live trading | `allow_live_trading: false` | LIVE orders denied unless explicitly enabled. |

All checks are enforced in `RiskManager.check_order()` and applied by the auto-trader and the webhook path. Risk state (kill switch, daily lock, high-water mark) is persisted to the `risk_state` table across restarts.

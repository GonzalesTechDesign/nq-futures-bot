# NQ Futures Trading Bot - Startup Guide

Setup and startup steps for the current FastAPI-based NQ Futures Trading Bot.

## 1. Configure Environment

1. Copy the environment template:
   ```bash
   cd /home/miggs101/Development/nq-futures-bot
   cp config/.env.example .env
   ```
2. Edit `.env`:
   - Set `DATABENTO_API_KEY` (optional — the auto-trader uses Yahoo Finance `NQ=F`; Databento is used only for history when a key is present).
   - Set `ADMIN_API_KEY` and `WEBHOOK_SECRET`, or leave them out and let the app generate random values on first run.
   - Keep `TRADING_MODE=PAPER` (LIVE is blocked by default).

## 2. Database Permission

The app uses SQLite at `nq_bot.db`. The user running the server must be able to write to it. If the file was created under a different user (e.g. root from `sudo`), fix ownership:

```bash
sudo chown $USER:$USER nq_bot.db
```

Without write permission, P&L recording and risk-state persistence degrade.

## 3. Start the Server

```bash
cd /home/miggs101/Development/nq-futures-bot
source venv/bin/activate
python run_server.py          # port 8888 (default), host 0.0.0.0
```

For TradingView webhooks arriving on port 80 (webhook URLs must use port 80 or 443):

```bash
sudo python run_server.py --port 80
```

Ports below 1024 require root.

## 4. Open the Dashboard

- API docs (Swagger UI): `http://localhost:8888/docs`
- Dashboard: `http://localhost:8888/` (served from `frontend/`)

The dashboard shows status, positions, P&L, DLL remaining, buffer zone, consistency, Trades Today, profit-target progress, TradingView signals, settings, and live WebSocket updates.

## 5. TradingView Webhook Setup

In TradingView, create an alert whose message is plain text or JSON, and point it at:

```
http://YOUR_IP:8888/api/v1/webhook/tradingview?token=YOUR_WEBHOOK_SECRET
```

(or port 80 when run as root). Example payloads:

- Plain text: `BUY NQ 2 @18500`, `SELL MNQ`, `FLATTEN`
- JSON: `{"action":"BUY","symbol":"NQ","quantity":2,"price":18500,"token":"YOUR_WEBHOOK_SECRET"}`

Notes:
- TradingView cannot send custom headers, so the shared secret is passed via `?token=` or a `token` body field.
- The endpoint returns HTTP 200 for accepted signals (executed or risk-rejected); it returns 4xx for invalid/missing auth, oversized, or rate-limited payloads.

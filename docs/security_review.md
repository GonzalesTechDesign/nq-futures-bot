# Security Review: NQ Futures Trading Bot

This document describes the post-fix security posture of the NQ Futures Trading Bot as it currently exists in `backend/` and `config/`.

## 1. Secrets & Credential Management

- Secrets live only in `.env` (never committed). The template is `config/.env.example`.
- `ADMIN_API_KEY` and `WEBHOOK_SECRET` are **rotated** from previous values. If not present in `.env`, the app generates random secrets on first run.
- `.gitignore` excludes local secrets and sensitive artifacts:
  - `*.db` (SQLite database holds raw webhook payloads / trade history)
  - `*.log` (server logs may contain webhook payloads)
  - `.env`
- Databento API key is optional and loaded from `DATABENTO_API_KEY` via the environment.

## 2. Request Logging & Stored Payloads

- Webhook payloads are truncated to a bounded size before logging (`_sanitize_log_payload`).
- Authentication tokens are **redacted** from stored payloads (`_redact_token`) before writing to the `tv_signals` table, and never appear in logs.
- Webhook body size is limited to 8 KB, and a per-IP rate limit (10 / 60 s) is enforced.

## 3. Endpoint Authorization

- **Control / backtest / reconciliation / signal-list** endpoints require the `X-API-Key` header (`ADMIN_API_KEY`), enforced by `verify_api_key` in `backend/auth.py`. This includes the backtest endpoints, which are now admin-authed.
- The **TradingView webhook** authenticates via the shared secret (`WEBHOOK_SECRET`) supplied as `?token=` or a body `token` field.

## 4. Transport-Level Controls

- **CORS** is restricted to loopback origins (`localhost:8888`, `127.0.0.1:8888`, `localhost:80`), overridable via `CORS_ORIGINS`.
- **WebSocket** (`/ws`): tokenless loopback connections are accepted for a locally-served dashboard; non-loopback connections must present the token, otherwise they are closed (code 1008).

## 5. Live Trading Is Blocked

- `allow_live_trading: false` in `config/risk_config.yaml` means no real-money orders are sent.
- `LIVE` mode requires both the config flag **and** the `ALLOW_LIVE_TRADING` environment variable; otherwise the API returns HTTP 403.

## 6. Risk & State Persistence

- RiskManager enforces: **max 4 contracts**, DLL **$1,200**, trailing MLL **$2,000**, buffer zones **50/75/90**, **max 6 trades/day**, and **$500/trade** risk.
- Risk state (kill switch, daily lock, drawdown high-water mark) is persisted to the `risk_state` table so it survives restarts.

## 7. Operational Caveats

- **Restart required for secret rotation.** `ADMIN_API_KEY` and `WEBHOOK_SECRET` are read at process start; the server must be restarted to pick up rotated values.
- **Database write permission.** The server process must be able to write to `nq_bot.db`; if it was created under a different user (e.g. root), change ownership so persistence works.
- Running on port 80 requires root (`sudo python run_server.py --port 80`).

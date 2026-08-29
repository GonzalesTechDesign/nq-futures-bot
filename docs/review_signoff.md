# Verification Report: NQ Futures Trading Bot

This report documents the current state of the NQ Futures Trading Bot as verified against the actual source code and test suite.

## 1. Risk Checks Are Enforced

All the eval risk limits defined in `config/risk_config.yaml` are enforced at order time via `RiskManager.check_order()` (`backend/risk_manager.py`). The enforcement path is used by both the background auto-trader and the TradingView webhook:

- Max contracts per symbol (4 minis / 40 micros).
- LIVE orders denied with HTTP 403 unless `allow_live_trading: true` is in config AND the `ALLOW_LIVE_TRADING` env var is set.
- Buffer zones scale position size (50% warning / 75% critical / 90% danger) and lock trading entirely in the danger zone.
- Daily trade cap (6 / day).
- Daily loss limit ($1,200 soft pause).
- Trailing drawdown / cumulative loss limit ($2,000 hard breach).
- Per-trade risk cap ($500).
- Consecutive-loss cooldown and kill switch.

## 2. State Persistence

- **Risk state** (`risk_state` table): kill switch, daily lock, trailing-drawdown high-water mark, trade counts, and cooldown are persisted via `RiskManager.save_state()` / `load_state()`, so a restart cannot silently clear a kill or a daily lock.
- **Paper state** (`PaperTradingState`) is the single source of truth for positions and P&L during a session. The DB `pnl_records` table is a backup read path for dashboard reloads/restarts.
- **Kill-switch persistence**: once the account is killed by a hard-breach, it stays killed across restarts until explicitly reset.

## 3. Server

- `run_server.py` defaults to port `8888` on host `0.0.0.0`; `--port 80` is available when run as root (for TradingView webhooks). This matches the API contract and dashboard CORS origins (`localhost:8888` / `127.0.0.1:8888`).

## 4. Backtest: Walk Forward Over Seeded Synthetic Windows

`WalkForwardValidator` (`backend/backtest_runner.py`) and `BacktestEngine` (`backend/backtest_engine.py`) run a **walk-forward validation of 5 independent seeded windows on synthetic price data**. Each window is generated from a seeded random walk with a unique seed and is evaluated independently against the eval rules (profit target, DLL/MLL, consistency). The response reports per-window and aggregate metrics plus a pass rate (e.g. `"0/5"`).

This is **not** a purged cross-validation on CME market data, and no specific Sharpe figure (e.g. 1.74) is claimed. The backtest exists to sanity-check that the strategy logic and eval rule enforcement work end-to-end, not to forecast real-market forward performance.

## 5. Test Suite: 80/80 Passing

The test suite under `tests/` (`test_backend.py`, `test_webhook.py`, `test_production.py`) passes **80/80** when run with a writable database:

```
DB_PATH="sqlite://///tmp/test_nqbot.db" venv/bin/python -m pytest tests/ -q
# 80 passed
```

Coverage includes risk/kill-switch behavior, rollover, webhook parsing/auth/rate-limit/token-redaction, backtest metric production, and endpoint authorization.

## 6. Known Limitations

- **Backtest is synthetic-data based.** It is a seeded simulation over generated random walks, not a live-data walk-forward or purged cross-validation on CME data. Results are indicative of logic correctness only.
- **IBKR broker execution is not live in the served app.** `IBKRClient.connect()` is bypassed when the app runs inside the FastAPI event loop, so order placement returns simulated IDs and paper execution is not a true IBKR paper account.
- **Databento is optional and not streaming.** A valid `DATABENTO_API_KEY` enables a historical client; the live auto-trader uses Yahoo Finance hourly data with a synthetic fallback. There is no real-time Databento streaming in this app.
- **Secrets require restart to take effect.** Rotated `ADMIN_API_KEY` / `WEBHOOK_SECRET` are read at process start; the server must be restarted to pick them up.
- **Database must be writable.** The server process needs write access to `nq_bot.db` (e.g. `chown` if the file was created under root); otherwise risk-state persistence and P&L recording degrade.

## 7. Sign-off Status

No sign-offs are claimed here. Live trading remains **blocked** by default configuration.

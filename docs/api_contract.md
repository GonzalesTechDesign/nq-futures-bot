# NQ Futures Trading Bot - API Contract & Architecture Specification

## Overview
This document defines the contract between the NautilusTrader Python backend engine and the Frontend dashboard, as well as configuration standards, risk limits, and contract rollover protocols.

---

## 1. Environment & Configuration
- **Default Mode:** Paper Trading (`IBKR port 7497`, TWS / IB Gateway).
- **Live Trading:** Strictly prohibited unless explicitly authorized by the user with sign-off from `Security-Agent` and `Review-Agent`.
- **Credentials:** Stored exclusively in environment variables (`.env`) or secure secrets manager. Never committed or logged.

---

## 2. API Endpoints (FastAPI Backend)

### Base URL: `http://localhost:8000/api/v1`

#### `GET /status`
Returns bot execution status, mode, and health.
**Response:**
```json
{
  "status": "RUNNING", // STOPPED, RUNNING, ERROR, KILLED
  "mode": "PAPER", // PAPER, LIVE
  "timestamp": "2026-08-14T12:00:00Z",
  "ibkr_connected": true,
  "databento_connected": true,
  "active_contract": "NQU6",
  "days_to_expiration": 18,
  "rollover_pending": false
}
```

#### `GET /positions`
Returns current open NQ positions and margin usage.
**Response:**
```json
{
  "account_id": "DU1234567",
  "net_liquidation": 100540.00,
  "margin_used": 15000.00,
  "margin_available": 85540.00,
  "positions": [
    {
      "symbol": "NQU6",
      "quantity": 2,
      "avg_px": 18550.25,
      "unrealized_pnl": 450.00,
      "realized_pnl": 120.00
    }
  ]
}
```

#### `GET /pnl`
Returns historical P&L summary and daily metrics.
**Response:**
```json
{
  "daily_pnl": 570.00,
  "total_pnl": 3420.00,
  "max_drawdown": -1250.00,
  "win_rate": 0.58
}
```

#### `GET /trades`
Returns trade execution log with timestamps and reasoning.
**Response:**
```json
{
  "trades": [
    {
      "trade_id": "trd_001",
      "timestamp": "2026-08-14T09:30:15Z",
      "symbol": "NQU6",
      "side": "BUY",
      "quantity": 2,
      "price": 18545.00,
      "reason": "Walk-forward ML model prediction p > 0.65 (momentum feature threshold met)",
      "order_type": "LIMIT"
    }
  ]
}
```

#### `GET /backtest/results`
Returns walk-forward validation results and performance metrics.
**Response:**
```json
{
  "strategy_name": "NQ_Momentum_WF_v1",
  "validation_method": "walk_forward_purged",
  "windows": [
    { "train_start": "2024-01-01", "train_end": "2024-06-30", "test_start": "2024-07-01", "test_end": "2024-09-30", "sharpe": 1.85, "return_pct": 14.2 },
    { "train_start": "2024-04-01", "train_end": "2024-09-30", "test_start": "2024-10-01", "test_end": "2024-12-31", "sharpe": 1.62, "return_pct": 11.5 }
  ],
  "aggregate_sharpe": 1.74,
  "aggregate_max_dd": -4.2
}
```

#### `POST /control/start`
Starts the trading bot.
**Request Body:**
```json
{
  "mode": "PAPER" // Must match config or explicit confirm for live
}
```

#### `POST /control/stop`
Stops the trading bot and flattens positions if requested.
**Request Body:**
```json
{
  "flatten": true
}
```

#### `POST /control/toggle-mode`
Toggles between paper and live (requires security checks & user auth).

---

## 3. WebSocket Feed (`/ws`)
Broadcasts real-time events:
- `tick`: price updates
- `order_update`: fills/rejections
- `pnl_update`: mark-to-market P&L
- `risk_alert`: margin warnings or drawdown kill-switch activation
- `rollover_notice`: proximity to front-month expiration

---

## 4. Contract Rollover Protocol
1. **Detection:** Front-month NQ contract expiration date is tracked automatically via NautilusTrader instrument definitions.
2. **Threshold:** 5 trading days prior to expiration (third Friday of contract month: March, June, September, December), bot flags `rollover_pending = true`.
3. **Action:**
   - Cease opening new positions in expiring contract.
   - Close existing positions at VWAP / TWAP or limit spread order.
   - Switch active contract subscription to next quarter front-month (e.g., NQU6 → NQZ6).
   - Log all roll actions with detailed audit trail.

---

## 5. Risk Controls (First-Class Config)
- `max_contracts`: Maximum allowable position size (default: 3 NQ contracts).
- `max_daily_loss`: Hard stop limit per day ($2,500 default).
- `max_drawdown_kill_switch`: Automatic shutdown and flattening if peak-to-trough drawdown exceeds 5%.
- `margin_utilization_limit`: Triggers warning at 70%, liquidation at 85%.

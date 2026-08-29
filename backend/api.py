import os
import re
import secrets
import time
import uuid
import json
import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, List, Set, Optional
from fastapi import FastAPI, HTTPException, Depends, Request, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from datetime import datetime, timezone
from pydantic import BaseModel
from shared.schema import (
    BotStatus,
    AccountPositions,
    PositionItem,
    PnLSummary,
    TradeLogItem,
    TradeLogResponse,
    BacktestResultResponse,
    SingleBacktestResultResponse,
    ControlStartRequest,
    ControlStopRequest,
    TradingViewSignalResponse,
    TradingViewSignalListResponse,
    TradingViewTestRequest,
)
from backend.risk_manager import RiskManager
from backend.rollover import ContractRolloverManager
from backend.strategy import NQMomentumStrategy
from backend.backtest_runner import WalkForwardValidator
from backend.database import init_db, SessionLocal, DBTrade, DBPosition, DBPnLRecord, DBTradingViewSignal
from backend.paper_trader import PaperTradingState
from backend.auto_trader import AutoTrader
from backend.auth import verify_api_key
from backend.ibkr_client import IBKRClient
from backend.databento_client import DatabentoClient
from backend.reconciliation import BrokerReconciliation

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("APIServer")

# ── Webhook secret ────────────────────────────────────────────────────────────
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
if not WEBHOOK_SECRET:
    WEBHOOK_SECRET = secrets.token_urlsafe(24)
    os.environ["WEBHOOK_SECRET"] = WEBHOOK_SECRET
    logger.warning("No WEBHOOK_SECRET set — generated random secret. Set it in .env for persistence.")

# ── Webhook security limits ──────────────────────────────────────────────────
WEBHOOK_MAX_BODY_BYTES = 8192          # 8 KB max payload
WEBHOOK_RATE_LIMIT_MAX = 10            # max signals per window
WEBHOOK_RATE_LIMIT_WINDOW = 60.0       # window size in seconds

# In-memory rate limiter: {ip: [timestamp, ...]}
_webhook_rate_buckets: Dict[str, List[float]] = {}


def _check_webhook_rate_limit(client_ip: str) -> Optional[str]:
    """Return None if request is allowed, or an error message if rate-limited."""
    now = time.monotonic()
    bucket = _webhook_rate_buckets.setdefault(client_ip, [])
    # Purge entries older than the window
    cutoff = now - WEBHOOK_RATE_LIMIT_WINDOW
    bucket[:] = [t for t in bucket if t > cutoff]
    if len(bucket) >= WEBHOOK_RATE_LIMIT_MAX:
        return f"Rate limit exceeded: {WEBHOOK_RATE_LIMIT_MAX} signals per {WEBHOOK_RATE_LIMIT_WINDOW:.0f}s"
    bucket.append(now)
    return None


def _redact_dict_tokens(data: Any) -> Any:
    """Recursively scrub token-bearing keys from a parsed payload structure.

    Walks dicts and lists at any depth and returns a NEW redacted structure
    (the input is not mutated):
      - any key named 'token' (case-insensitive) is removed entirely;
      - any OTHER key ending in 'token' (e.g. 'api_token') keeps its name but
        its string value is replaced with a redaction placeholder.
    """
    if isinstance(data, dict):
        cleaned = {}
        for key, value in data.items():
            if str(key).lower() == "token":
                # Drop the auth-secret key entirely at any depth.
                continue
            if isinstance(value, str) and str(key).lower().endswith("token"):
                # Token-ish key (e.g. 'api_token'): keep the key, redact value.
                cleaned[key] = "[REDACTED]"
            else:
                cleaned[key] = _redact_dict_tokens(value)
        return cleaned
    if isinstance(data, list):
        return [_redact_dict_tokens(item) for item in data]
    return data


def _sanitize_log_payload(raw: str, max_len: int = 500) -> str:
    """Truncate raw payload for safe logging (no injection, no secrets).

    Authentication tokens must never reach the logs. JSON payloads are
    redacted recursively (any 'token' key at any depth is removed; other
    token-ending keys are scrubbed); non-JSON payloads are scrubbed
    defensively for `token=...` and `token: ...` fragments (same strategy as
    `_redact_token`, which is used for persisted payloads).
    """
    payload = (raw or "").strip()
    if not payload:
        return payload

    try:
        data = json.loads(payload)
        if isinstance(data, (dict, list)):
            data = _redact_dict_tokens(data)
            # Re-serialize the redacted structure before truncating.
            payload = json.dumps(data)
        else:
            raise ValueError("not a JSON object")
    except (json.JSONDecodeError, ValueError, TypeError):
        # Non-JSON (or non-object) payload: scrub token fragments defensively.
        payload = re.sub(
            r"\?&?token=[^&\s\"]*", "token=***REDACTED***", payload
        )
        payload = re.sub(
            r'"token"\s*:\s*"[^"]*"', '"token":"***REDACTED***"', payload
        )
        payload = re.sub(
            r"(?i)(\btoken\s*[=:]\s*)[^\s,&'\"}]+",
            r"\1[REDACTED]",
            payload,
        )

    sanitized = payload[:max_len]
    if len(payload) > max_len:
        sanitized += f"... ({len(payload)} bytes total)"
    return sanitized


def _redact_token(raw_payload: str) -> str:
    """Strip authentication tokens from a payload before it is persisted.

    JSON payloads are redacted recursively (any 'token' key at any depth is
    removed); plaintext payloads are scrubbed defensively for token=... /
    token:... fragments.
    """
    payload = (raw_payload or "").strip()
    if not payload:
        return payload
    try:
        data = json.loads(payload)
        if isinstance(data, (dict, list)):
            return json.dumps(_redact_dict_tokens(data))
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return re.sub(
        r"(?i)(\btoken\s*[=:]\s*)[^\s,&'\"}]+",
        r"\1[REDACTED]",
        payload,
    )


FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

app = FastAPI(title="NQ Futures Bot — Lucid Trading Eval", version="2.0.0")

# ── CORS: restrict to loopback + configured dashboard origin(s) ───────────────
# The dashboard authenticates via the X-API-Key header (not cookies), so
# credentials are never needed; keep allow_credentials=False.
DEFAULT_CORS_ORIGINS = [
    "http://localhost:8888",
    "http://127.0.0.1:8888",
    "http://localhost:80",
]
_cors_env = os.getenv("CORS_ORIGINS", "")
CORS_ORIGINS = (
    [o.strip() for o in _cors_env.split(",") if o.strip()]
    if _cors_env
    else DEFAULT_CORS_ORIGINS
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize database
init_db()

risk_mgr = RiskManager()
rollover_mgr = ContractRolloverManager()
ibkr_client = IBKRClient()
databento_client = DatabentoClient()
reconciliation = BrokerReconciliation(ibkr_client)
strategy = NQMomentumStrategy(risk_mgr, rollover_mgr, ibkr_client, base_symbol="NQ", mode="PAPER")
wf_validator = WalkForwardValidator()
paper_state = PaperTradingState()
auto_trader = AutoTrader(strategy, paper_state, risk_mgr)

bot_state = {
    "status": "RUNNING",
    "mode": "PAPER",
    "ibkr_connected": False,
    "databento_connected": False,
    "active_symbol": "NQ"
}

# ── WebSocket connection manager ──────────────────────────────────────────────
ws_clients: Set[WebSocket] = set()


def _databento_connected() -> bool:
    """True only when the Databento client is live with a real API key."""
    return (
        databento_client.client is not None
        and bool(databento_client.api_key)
        and databento_client.api_key != "db-your-databento-api-key-here"
    )


@app.on_event("startup")
async def startup_event():
    logger.info("Starting NQ/MNQ Trading Bot API server...")
    # Restore persisted risk state (kills, daily locks, drawdown HWM, etc.)
    risk_mgr.load_state()
    # IBKR connect may fail due to event loop conflict — that's fine, it falls back to paper mode
    try:
        bot_state["ibkr_connected"] = ibkr_client.connect()
    except Exception as e:
        logger.warning(f"IBKR connect skipped (paper mode): {e}")
        bot_state["ibkr_connected"] = False
    bot_state["databento_connected"] = _databento_connected()
    try:
        strategy.on_start()
    except Exception as e:
        logger.warning(f"Strategy start skipped: {e}")
    # Start the WebSocket broadcast loop in the background
    asyncio.create_task(_ws_broadcast_loop())

# ── WebSocket broadcast loop ──────────────────────────────────────────────────
async def _ws_broadcast_loop():
    """Push dashboard data to all connected WebSocket clients every 2 seconds."""
    while True:
        if ws_clients:
            payload = _build_dashboard_payload()
            dead: list[WebSocket] = []
            for ws in list(ws_clients):
                try:
                    await ws.send_text(json.dumps(payload, default=str))
                except Exception:
                    dead.append(ws)
            for ws in dead:
                ws_clients.discard(ws)
        await asyncio.sleep(2)


def _build_dashboard_payload() -> dict:
    """Assemble the full dashboard payload (shared by WS and REST)."""
    rollover = rollover_mgr.check_rollover_status(bot_state["active_symbol"])

    # --- Status ---
    status = {
        "status": bot_state["status"],
        "mode": bot_state["mode"],
        "ibkr_connected": ibkr_client.connected,
        "databento_connected": bot_state["databento_connected"],
        "active_contract": rollover["active_contract"],
        "days_to_expiration": rollover["days_to_expiration"],
        "rollover_pending": rollover["rollover_pending"],
    }

    # --- Positions (IBKR live, or paper trading state) ---
    # Fetch latest price from auto_trader for real-time P&L calculation
    current_price = auto_trader._last_price
    ps = paper_state.get_state(current_price=current_price)
    if ibkr_client.connected:
        try:
            account_summary = ibkr_client.get_account_summary()
            raw_positions = ibkr_client.get_positions()
            account_summary["net_liquidation"] = ps["equity"]  # always use paper equity
        except Exception:
            ibkr_client.connected = False  # degrade gracefully
            account_summary = {
                "net_liquidation": ps["equity"],
                "margin_used": 0.0,
                "margin_available": ps["equity"],
            }
            raw_positions = [
                {
                    "symbol": sym,
                    "quantity": info["qty"],
                    "avg_price": info["entry_price"],
                    "unrealized_pnl": info.get("unrealized_pnl", 0.0),
                    "realized_pnl": ps["total_pnl"],
                }
                for sym, info in ps["open_positions"].items()
            ]
    else:
        account_summary = {
            "net_liquidation": ps["equity"],
            "margin_used": 0.0,
            "margin_available": ps["equity"],
        }
        raw_positions = [
            {
                "symbol": sym,
                "quantity": info["qty"],
                "avg_price": info["entry_price"],
                "unrealized_pnl": info.get("unrealized_pnl", 0.0),
                "realized_pnl": ps["total_pnl"],
            }
            for sym, info in ps["open_positions"].items()
        ]

    positions = [
        {
            "symbol": p["symbol"],
            "quantity": p["quantity"],
            "avg_px": p["avg_price"],
            "unrealized_pnl": p["unrealized_pnl"],
            "realized_pnl": p["realized_pnl"],
        }
        for p in raw_positions
    ]

    account_positions = {
        "account_id": "DU_PAPER_NQ_MNQ",
        "net_liquidation": account_summary["net_liquidation"],
        "margin_used": account_summary["margin_used"],
        "margin_available": account_summary["margin_available"],
        "positions": positions,
    }

    # --- P&L (paper_state is source of truth, DB is backup) ---
    # Include unrealized P&L from live price in total equity
    pnl = {
        "daily_pnl": ps["daily_pnl"],
        "total_pnl": ps["total_pnl"] + sum(p.get("unrealized_pnl", 0.0) for p in ps["open_positions"].values()),
        "max_drawdown": ps["max_drawdown"],
        "win_rate": ps["win_rate"],
    }
    # Supplement from DB if paper_state is fresh (e.g. server restart)
    db = SessionLocal()
    try:
        latest = db.query(DBPnLRecord).order_by(DBPnLRecord.timestamp.desc()).first()
        if latest:
            if pnl["total_pnl"] == 0.0 and latest.total_pnl != 0.0:
                pnl["daily_pnl"] = latest.daily_pnl
                pnl["total_pnl"] = latest.total_pnl
    finally:
        db.close()

    # --- DLL remaining (derived from risk config / P&L) ---
    cumulative_dll = getattr(
        risk_mgr, "cumulative_loss_limit", None
    ) or risk_mgr.config.get("trailing_drawdown_limit_usd", 2000.0)
    dll_used = max(0, -pnl["total_pnl"]) if pnl["total_pnl"] < 0 else 0.0
    dll_remaining = max(0.0, cumulative_dll - dll_used)

    # --- Buffer status (risk drawdown zones) ---
    buffer_pct = risk_mgr.get_mll_used_pct()
    if buffer_pct >= risk_mgr.buffer_danger_pct:
        buffer_status = "DANGER"
    elif buffer_pct >= risk_mgr.buffer_critical_pct:
        buffer_status = "CRITICAL"
    elif buffer_pct >= risk_mgr.buffer_warning_pct:
        buffer_status = "WARNING"
    else:
        buffer_status = "SAFE"
    max_position_size = risk_mgr.get_max_contracts_for_current_risk()

    # --- Trades today ---
    # risk_mgr.trades_today is the single source of truth (in-memory, restored
    # from risk_state on restart). A DB count of DBTrade rows since local
    # midnight can disagree with it due to flush timing / auto_trader legs.
    trades_today_count = risk_mgr.trades_today

    # --- Profit target progress ---
    profit_target = getattr(
        risk_mgr, "profit_target", None
    ) or risk_mgr.config.get("profit_target_usd", 3000.0)
    profit_progress = max(0.0, min(100.0, (pnl["total_pnl"] / profit_target) * 100)) if profit_target else 0.0

    # --- DLL progress ---
    dll_pct_used = max(0.0, min(100.0, (dll_used / cumulative_dll) * 100)) if cumulative_dll else 0.0

    # --- Consistency ---
    max_day_pct = risk_mgr.config.get("max_day_pct", 30.0)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "account": account_positions,
        "pnl": pnl,
        "dll": {
            "remaining": dll_remaining,
            "cumulative_limit": cumulative_dll,
            "pct_used": round(dll_pct_used, 1),
        },
        "buffer": {
            "status": buffer_status,
            "max_position_size": max_position_size,
        },
        "consistency": {
            "status": "COMPLIANT",
            "max_day_pct": max_day_pct,
        },
        "trades_today": trades_today_count,
        "profit_target": {
            "pct": round(profit_progress, 1),
            "target": profit_target,
        },
        "auto_trade": auto_trader.get_status(),
        "ngrok_host": os.getenv("NGROK_HOST", ""),
        "api_key_hint": (lambda k: k[:4] + "..." + k[-4:] if len(k) > 8 else "****")(os.getenv("ADMIN_API_KEY", "")),
    }


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/api/v1/dashboard")
def get_dashboard():
    """Single-call endpoint returning all dashboard data."""
    return _build_dashboard_payload()


@app.get("/api/v1/status", response_model=BotStatus)
def get_status():
    rollover = rollover_mgr.check_rollover_status(bot_state["active_symbol"])
    return BotStatus(
        status=bot_state["status"],
        mode=bot_state["mode"],
        timestamp=datetime.now(timezone.utc),
        ibkr_connected=ibkr_client.connected,
        databento_connected=bot_state["databento_connected"],
        active_contract=rollover["active_contract"],
        days_to_expiration=rollover["days_to_expiration"],
        rollover_pending=rollover["rollover_pending"]
    )

@app.get("/api/v1/positions", response_model=AccountPositions)
def get_positions():
    rollover = rollover_mgr.check_rollover_status(bot_state["active_symbol"])
    ps = paper_state.get_state()
    if ibkr_client.connected:
        try:
            summary = ibkr_client.get_account_summary()
            positions = ibkr_client.get_positions()
            summary["net_liquidation"] = ps["equity"]  # always use paper equity
        except Exception:
            ibkr_client.connected = False
            summary = {"net_liquidation": ps["equity"], "margin_used": 0.0, "margin_available": ps["equity"]}
            positions = [
                {
                    "symbol": sym,
                    "quantity": info["qty"],
                    "avg_price": info["entry_price"],
                    "unrealized_pnl": 0.0,
                    "realized_pnl": ps["total_pnl"],
                }
                for sym, info in ps["open_positions"].items()
            ]
    else:
        summary = {"net_liquidation": ps["equity"], "margin_used": 0.0, "margin_available": ps["equity"]}
        positions = [
            {
                "symbol": sym,
                "quantity": info["qty"],
                "avg_price": info["entry_price"],
                "unrealized_pnl": 0.0,
                "realized_pnl": ps["total_pnl"],
            }
            for sym, info in ps["open_positions"].items()
        ]

    pos_items = [
        PositionItem(
            symbol=p["symbol"],
            quantity=p["quantity"],
            avg_px=p["avg_price"],
            unrealized_pnl=p["unrealized_pnl"],
            realized_pnl=p["realized_pnl"]
        ) for p in positions
    ]

    return AccountPositions(
        account_id="DU_PAPER_NQ_MNQ",
        net_liquidation=summary["net_liquidation"],
        margin_used=summary["margin_used"],
        margin_available=summary["margin_available"],
        positions=pos_items
    )

@app.get("/api/v1/pnl", response_model=PnLSummary)
def get_pnl():
    ps = paper_state.get_state()
    db = SessionLocal()
    try:
        latest = db.query(DBPnLRecord).order_by(DBPnLRecord.timestamp.desc()).first()
        if latest:
            # Use DB values if paper_state hasn't moved yet (e.g. fresh restart)
            total = ps["total_pnl"] if ps["total_pnl"] != 0.0 else latest.total_pnl
            daily = ps["daily_pnl"] if ps["total_pnl"] != 0.0 else latest.daily_pnl
            return PnLSummary(
                daily_pnl=daily,
                total_pnl=total,
                max_drawdown=ps["max_drawdown"],
                win_rate=ps["win_rate"],
            )
    finally:
        db.close()

    return PnLSummary(
        daily_pnl=ps["daily_pnl"],
        total_pnl=ps["total_pnl"],
        max_drawdown=ps["max_drawdown"],
        win_rate=ps["win_rate"],
    )

@app.get("/api/v1/trades", response_model=TradeLogResponse)
def get_trades():
    db = SessionLocal()
    try:
        db_trades = db.query(DBTrade).order_by(DBTrade.timestamp.desc()).limit(50).all()
        if db_trades:
            items = [
                TradeLogItem(
                    trade_id=t.trade_id,
                    timestamp=t.timestamp,
                    symbol=t.symbol,
                    side=t.side,
                    quantity=t.quantity,
                    price=t.price,
                    reason=t.reason,
                    order_type=t.order_type
                ) for t in db_trades
            ]
            return TradeLogResponse(trades=items)
    finally:
        db.close()

    return TradeLogResponse(trades=[])

@app.get("/api/v1/reconciliation")
def run_reconciliation(_: str = Depends(verify_api_key)):
    # PaperTradingState is the source of truth for intended positions.
    paper_positions = {
        sym: (pos["qty"] if pos["side"] == "LONG" else -pos["qty"])
        for sym, pos in paper_state.open_positions.items()
        if pos.get("qty", 0) != 0
    }
    return reconciliation.reconcile_positions(paper_positions=paper_positions)

@app.get("/api/v1/backtest/results", response_model=BacktestResultResponse)
def get_backtest_results(_: str = Depends(verify_api_key), seed: int = Query(42)):
    res = wf_validator.run_walk_forward_validation(seed=seed)
    return BacktestResultResponse(**res)

@app.get("/api/v1/backtest/single", response_model=SingleBacktestResultResponse)
def get_single_backtest(_: str = Depends(verify_api_key), seed: int = Query(42)):
    """Run a single full-period backtest (no walk-forward split) for quick validation."""
    res = wf_validator.run_single_backtest(seed=seed)
    return SingleBacktestResultResponse(**res)

@app.post("/api/v1/control/start")
async def control_start(req: ControlStartRequest, _: str = Depends(verify_api_key)):
    if req.mode == "LIVE":
        # Double-gate: must have config AND env var
        if not risk_mgr.config.get("allow_live_trading", False):
            raise HTTPException(status_code=403, detail="Live trading mode not authorized by risk config.")
        if not os.getenv("ALLOW_LIVE_TRADING"):
            raise HTTPException(status_code=403, detail="Set ALLOW_LIVE_TRADING env var to enable live trading.")
    bot_state["mode"] = req.mode
    bot_state["status"] = "RUNNING"
    strategy.mode = req.mode
    # Start the auto-trader
    result = auto_trader.start()
    logger.info(f"Bot started in {req.mode} mode. Auto-trader: {result.get('status', 'unknown')}")
    return {"message": f"Bot started successfully in {req.mode} mode.", "status": bot_state["status"], "auto_trade": result}

@app.post("/api/v1/control/stop")
async def control_stop(req: ControlStopRequest, _: str = Depends(verify_api_key)):
    bot_state["status"] = "STOPPED"
    # Stop the auto-trader
    auto_trader.stop("ABORT BOT")
    if req.flatten:
        # Flatten any open position (paper_state is the authority)
        if paper_state.open_positions:
            for sym, pos in list(paper_state.open_positions.items()):
                if pos.get("qty", 0) == 0:
                    continue  # never record a zero-qty leg
                side = "SELL" if pos["side"] == "LONG" else "BUY"
                price = auto_trader._last_price or pos["entry_price"]
                pnl = paper_state.record_trade(
                    symbol=sym, side=side, qty=pos["qty"], price=price,
                    current_date=datetime.now().date(),
                )
                risk_mgr.record_trade(pnl)
                auto_trader._record_db_trade(side, pos["qty"], sym, price, "ABORT BOT flatten")
        strategy.position_qty = 0
        strategy.position_side = None
        strategy.entry_price = 0.0
        strategy.trailing_stop = 0.0
        strategy.bars_in_position = 0
        logger.info("Bot stopped, positions flattened.")
    else:
        logger.info("Bot stopped, positions held.")
    return {"message": "Bot stopped successfully.", "status": bot_state["status"], "flattened": req.flatten}


@app.post("/api/v1/paper/reset")
def paper_reset(_: str = Depends(verify_api_key)):
    """Reset the paper trading account to $50,000. Clears all positions and P&L."""
    paper_state.reset()
    strategy.position_qty = 0
    strategy.position_side = None
    strategy.entry_price = 0.0
    strategy.trailing_stop = 0.0
    strategy.stop_loss = 0.0
    strategy.take_profit = 0.0
    strategy.bars_in_position = 0
    strategy.bars_since_last_trade = 999
    risk_mgr.total_pnl = 0.0
    risk_mgr.daily_pnl = 0.0
    risk_mgr.current_equity = risk_mgr.account_size
    risk_mgr.peak_equity = risk_mgr.account_size
    risk_mgr.trades_today = 0
    risk_mgr.killed = False
    risk_mgr.kill_reason = ""
    risk_mgr._current_date = None
    risk_mgr.day_start_pnl = 0.0
    risk_mgr.day_start_equity = risk_mgr.account_size
    risk_mgr.consecutive_losses = 0
    risk_mgr.cooldown_remaining = 0
    risk_mgr.daily_blocked = False
    # Clear stale DB records so dashboard doesn't re-inject old P&L
    db = SessionLocal()
    try:
        db.query(DBPnLRecord).delete()
        db.query(DBTrade).delete()
        db.query(DBTradingViewSignal).delete()
        db.commit()
    finally:
        db.close()
    # Persist the fresh risk state so the reset survives a restart
    risk_mgr.save_state()
    logger.info("Paper trading account and risk state reset.")
    return {"message": "Paper account reset to $50,000.", "equity": paper_state.equity}


# ── Auto-Trading Endpoints ────────────────────────────────────────────────

@app.get("/api/v1/autotrade/status")
def autotrade_status():
    """Get auto-trading status."""
    return auto_trader.get_status()


@app.post("/api/v1/autotrade/start")
async def autotrade_start(_: str = Depends(verify_api_key)):
    """Start auto-trading. Targets $60,000, hard stop at $49,500."""
    result = auto_trader.start()
    return result


@app.post("/api/v1/autotrade/stop")
async def autotrade_stop(_: str = Depends(verify_api_key)):
    """Stop auto-trading."""
    result = auto_trader.stop("manual stop via API")
    return result


# ── TradingView Webhook Endpoints ────────────────────────────────────────────

def _verify_webhook_token(token: Optional[str] = None, body_token: Optional[str] = None) -> bool:
    """Verify the webhook shared secret. Checks query param or body field."""
    provided = token or body_token
    if not provided:
        return False
    return secrets.compare_digest(provided, WEBHOOK_SECRET)


def _parse_tv_payload(raw: str) -> dict:
    """
    Parse a TradingView alert payload.
    Supports JSON and plain text: "BUY NQ 2 @18500"
    Returns a dict with keys: action, symbol, quantity, price, strategy, alert_name.
    """
    raw = raw.strip()

    # Try JSON first
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            action = str(data.get("action", "")).upper().strip()
            symbol = str(data.get("symbol", "NQ")).upper().strip()
            qty = int(data.get("quantity", 1))
            price = float(data["price"]) if data.get("price") is not None else None
            return {
                "action": action,
                "symbol": symbol,
                "quantity": qty,
                "price": price,
                "strategy": data.get("strategy"),
                "alert_name": data.get("alert_name"),
                "token": data.get("token"),
            }
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # Plain text: "BUY NQ 2 @18500" or "SELL NQ 1"
    # Also handles: "BUY 2 @18500" (default symbol NQ), "SELL MNQ" (symbol at end of string)
    pattern = r"^(BUY|SELL|FLATTEN)\s+(?:([A-Z]+)\s*)?(\d+)?(?:\s*@([\d.]+))?"
    m = re.match(pattern, raw.upper())
    if m:
        action = m.group(1)
        symbol = m.group(2) or "NQ"
        qty = int(m.group(3)) if m.group(3) else 1
        price = float(m.group(4)) if m.group(4) else None
        return {
            "action": action,
            "symbol": symbol,
            "quantity": qty,
            "price": price,
            "strategy": None,
            "alert_name": None,
            "token": None,
        }

    raise ValueError(f"Unable to parse TradingView payload: {raw[:100]}")


def _record_tv_signal(
    db,
    *,
    raw_payload: str,
    action: str,
    symbol: str,
    qty: int,
    status: str,
    reject_reason: str = None,
    execution_price: float = None,
    strategy_name: str = None,
    alert_name: str = None,
) -> DBTradingViewSignal:
    """Insert a row into tv_signals and return it."""
    sig = DBTradingViewSignal(
        raw_payload=_redact_token(raw_payload)[:4096],
        parsed_action=action,
        parsed_symbol=symbol,
        parsed_qty=qty,
        status=status,
        reject_reason=reject_reason,
        execution_price=execution_price,
        strategy_name=strategy_name,
        alert_name=alert_name,
    )
    db.add(sig)
    db.commit()
    db.refresh(sig)
    return sig


@app.post("/api/v1/webhook/tradingview")
async def tradingview_webhook(request: Request, token: Optional[str] = Query(None)):
    """
    Receive a TradingView alert webhook.

    Authentication: pass the shared secret as ?token=xxx query param
    (TradingView cannot send custom headers).

    Accepts:
      - JSON body: {"action":"BUY","symbol":"NQ","quantity":2,"price":18500,...}
      - Plain text body: "BUY NQ 2 @18500"
    """
    db = SessionLocal()
    try:
        # ── Rate limit ────────────────────────────────────────────────────
        # Trust the last X-Forwarded-For hop ONLY when the immediate peer is
        # loopback (direct proxy); otherwise rate-limit on the peer itself.
        client_ip = request.client.host if request.client else "unknown"
        xff = request.headers.get("x-forwarded-for", "")
        if xff and client_ip in ("127.0.0.1", "::1", "localhost"):
            client_ip = xff.split(",")[-1].strip()
        rate_err = _check_webhook_rate_limit(client_ip)
        if rate_err:
            return JSONResponse(status_code=429, content={"status": "error", "message": rate_err})

        # ── Read raw body ─────────────────────────────────────────────────
        raw_body = (await request.body()).decode("utf-8", errors="replace")

        # ── Payload size limit (must be BEFORE any parsing) ───────────────
        if len(raw_body.encode("utf-8")) > WEBHOOK_MAX_BODY_BYTES:
            logger.warning(f"Webhook rejected: payload too large ({len(raw_body.encode('utf-8'))} bytes) from {client_ip}")
            return JSONResponse(status_code=413, content={"status": "error", "message": "Payload too large (max 8KB)"})

        # Log sanitized payload (truncated, token scrubbed) — NEVER log the raw token
        _log_payload = _sanitize_log_payload(raw_body)
        logger.info(f"Webhook received from {client_ip}: {_log_payload}")

        # Parse payload
        try:
            parsed = _parse_tv_payload(raw_body)
        except ValueError as e:
            # Log the unparseable signal
            _record_tv_signal(
                db, raw_payload=raw_body, action="UNKNOWN", symbol="UNKNOWN",
                qty=0, status="BLOCKED", reject_reason=str(e),
            )
            raise HTTPException(status_code=400, detail={"error": {"code": "PARSE_ERROR", "message": str(e)}})

        # Authentication
        body_token = parsed.pop("token", None)
        if not _verify_webhook_token(token=token, body_token=body_token):
            _record_tv_signal(
                db, raw_payload=raw_body, action=parsed["action"], symbol=parsed["symbol"],
                qty=parsed["quantity"], status="BLOCKED", reject_reason="Invalid or missing webhook token",
                strategy_name=parsed.get("strategy"), alert_name=parsed.get("alert_name"),
            )
            raise HTTPException(status_code=401, detail={"error": {"code": "AUTH_FAILED", "message": "Invalid or missing webhook token."}})

        action = parsed["action"]
        symbol = parsed["symbol"]
        qty = parsed["quantity"]
        price = parsed.get("price")
        strategy_name = parsed.get("strategy")
        alert_name = parsed.get("alert_name")

        # FLATTEN: flatten the entire position
        if action == "FLATTEN":
            # paper_state is the authority for the open position; fall back to
            # strategy.position_qty only if no real paper position exists.
            paper_pos = paper_state.open_positions.get(symbol)
            if paper_pos is not None and paper_pos.get("qty", 0) != 0:
                current_qty = paper_pos["qty"] * (1 if paper_pos["side"] == "LONG" else -1)
            else:
                paper_pos = None
                current_qty = strategy.position_qty
            if current_qty == 0:
                sig = _record_tv_signal(
                    db, raw_payload=raw_body, action=action, symbol=symbol, qty=0,
                    status="EXECUTED", execution_price=price,
                    strategy_name=strategy_name, alert_name=alert_name,
                )
                return {"status": "EXECUTED", "action": "FLATTEN", "message": "Already flat.", "signal_id": sig.id}

            # Capture the original entry price BEFORE clearing strategy state
            # (B5: a price-less FLATTEN must not fill at a hardcoded price).
            entry_price = (
                paper_pos["entry_price"]
                if paper_pos is not None
                else strategy.entry_price
            ) or 0.0

            # Close the position
            exit_side = "SELL" if current_qty > 0 else "BUY"
            exit_qty = abs(current_qty)

            # Update strategy state
            strategy.position_qty = 0
            strategy.position_side = None
            strategy.entry_price = 0.0
            strategy.trailing_stop = 0.0
            strategy.stop_loss = 0.0
            strategy.take_profit = 0.0
            strategy.bars_in_position = 0
            strategy.bars_since_last_trade = 0

            # Record in DB
            fill_price = price if price else entry_price
            trade_id = f"tv_{uuid.uuid4().hex[:12]}"
            db_trade = DBTrade(
                trade_id=trade_id,
                symbol=symbol,
                side=exit_side,
                quantity=exit_qty,
                price=fill_price,
                reason=f"TradingView FLATTEN webhook ({alert_name or 'manual'})",
                order_type="MARKET",
            )
            db.add(db_trade)
            db.commit()

            sig = _record_tv_signal(
                db, raw_payload=raw_body, action=action, symbol=symbol, qty=exit_qty,
                status="EXECUTED", execution_price=fill_price,
                strategy_name=strategy_name, alert_name=alert_name,
            )

            # ── Record in paper trading state & P&L snapshot ────────
            # Only record a real paper leg when there was an open paper position.
            if paper_pos is not None:
                trade_pnl = paper_state.record_trade(symbol, exit_side, exit_qty, fill_price)
                risk_mgr.record_trade(trade_pnl)
                db_pnl = DBPnLRecord(
                    daily_pnl=paper_state.daily_pnl,
                    total_pnl=paper_state.total_pnl,
                    net_liquidation=paper_state.equity,
                    margin_used=0.0,
                )
                db.add(db_pnl)
                db.commit()
            else:
                trade_pnl = 0.0

            logger.info(
                f"FLATTEN executed: closed {exit_qty} {symbol} @ {fill_price} "
                f"(signal #{sig.id}, P&L=${trade_pnl:+.2f})"
            )
            return {
                "status": "EXECUTED",
                "action": "FLATTEN",
                "exit_side": exit_side,
                "quantity": exit_qty,
                "price": fill_price,
                "signal_id": sig.id,
                "trade_id": trade_id,
                "pnl": trade_pnl,
            }

        # BUY or SELL
        if action not in ("BUY", "SELL"):
            _record_tv_signal(
                db, raw_payload=raw_body, action=action, symbol=symbol, qty=qty,
                status="BLOCKED", reject_reason=f"Unknown action: {action}",
                strategy_name=strategy_name, alert_name=alert_name,
            )
            raise HTTPException(status_code=400, detail={"error": {"code": "INVALID_ACTION", "message": f"Action must be BUY, SELL, or FLATTEN. Got: {action}"}})

        # Quantity must be positive
        if qty <= 0:
            _record_tv_signal(
                db, raw_payload=raw_body, action=action, symbol=symbol, qty=qty,
                status="REJECTED", reject_reason=f"Invalid quantity: {qty}",
                strategy_name=strategy_name, alert_name=alert_name,
            )
            raise HTTPException(status_code=400, detail={"error": {"code": "INVALID_QTY", "message": f"Quantity must be positive. Got: {qty}"}})

        # Compute signed quantity for risk check (BUY=+, SELL=-)
        signed_qty = qty if action == "BUY" else -qty

        # Reject absurd orders early — a single order can never exceed the
        # buffer-adjusted max (B7). check_order() would also block it, but this
        # gives a precise reason and skips execution entirely.
        max_for_risk = risk_mgr.get_max_contracts_for_current_risk()
        if qty > max_for_risk:
            reject_msg = f"Position size {qty} exceeds max {max_for_risk}"
            sig = _record_tv_signal(
                db, raw_payload=raw_body, action=action, symbol=symbol, qty=qty,
                status="REJECTED", reject_reason=reject_msg,
                strategy_name=strategy_name, alert_name=alert_name,
            )
            logger.warning(f"TV signal REJECTED (oversize): {action} {qty} {symbol} — {reject_msg} (signal #{sig.id})")
            return {
                "status": "REJECTED",
                "action": action,
                "quantity": qty,
                "symbol": symbol,
                "reject_reason": reject_msg,
                "signal_id": sig.id,
            }

        # Risk check — returns False with a log reason on failure
        approved = risk_mgr.check_order(signed_qty, strategy.position_qty, bot_state["mode"])
        if not approved:
            # Figure out which risk rule blocked it (check in same order as check_order)
            reject_reason = _get_risk_reject_reason(signed_qty)
            sig = _record_tv_signal(
                db, raw_payload=raw_body, action=action, symbol=symbol, qty=qty,
                status="REJECTED", reject_reason=reject_reason,
                strategy_name=strategy_name, alert_name=alert_name,
            )
            logger.warning(f"TV signal REJECTED (risk): {action} {qty} {symbol} — {reject_reason} (signal #{sig.id})")
            return {
                "status": "REJECTED",
                "action": action,
                "quantity": qty,
                "symbol": symbol,
                "reject_reason": reject_reason,
                "signal_id": sig.id,
            }

        # Execute — place order (paper mode: simulate fill if IBKR not connected)
        # HARD STOP: block trade if equity below the eval config hard stop
        if paper_state.equity <= auto_trader.hard_stop_equity:
            sig = _record_tv_signal(
                db, raw_payload=raw_body, action=action, symbol=symbol, qty=qty,
                status="BLOCKED", reject_reason=f"HARD STOP: equity ${paper_state.equity:,.2f} <= ${auto_trader.hard_stop_equity:,.2f}",
            )
            return JSONResponse(status_code=403, content={
                "status": "BLOCKED",
                "reject_reason": f"Trading blocked: equity ${paper_state.equity:,.2f} is below hard stop ${auto_trader.hard_stop_equity:,.2f}",
            })
        fill_price = price if price else 18500.0  # fallback if TV didn't send price
        sl, tp = risk_mgr.calculate_protective_stops(fill_price, action)
        order_id = strategy.ibkr_client.place_bracket_order(symbol, action, qty, fill_price, sl, tp)

        # Record trade in DB
        trade_id = f"tv_{uuid.uuid4().hex[:12]}"
        db_trade = DBTrade(
            trade_id=trade_id,
            symbol=symbol,
            side=action,
            quantity=qty,
            price=fill_price,
            reason=f"TradingView webhook: {alert_name or strategy_name or 'TV signal'}",
            order_type="MARKET",
        )
        db.add(db_trade)
        db.commit()

        sig = _record_tv_signal(
            db, raw_payload=raw_body, action=action, symbol=symbol, qty=qty,
            status="EXECUTED", execution_price=fill_price,
            strategy_name=strategy_name, alert_name=alert_name,
        )

        # ── Record in paper trading state & P&L snapshot ────────────
        trade_pnl = paper_state.record_trade(symbol, action, qty, fill_price)
        risk_mgr.record_trade(trade_pnl)
        db_pnl = DBPnLRecord(
            daily_pnl=paper_state.daily_pnl,
            total_pnl=paper_state.total_pnl,
            net_liquidation=paper_state.equity,
            margin_used=0.0,
        )
        db.add(db_pnl)
        db.commit()

        # Derive strategy state from paper_state AFTER record_trade (B6) —
        # paper_state is the authority, so we never independently increment.
        pos = paper_state.open_positions.get(symbol)
        if pos is not None and pos.get("qty", 0) != 0:
            strategy.position_qty = pos["qty"] if pos["side"] == "LONG" else -pos["qty"]
            strategy.position_side = pos["side"]
            strategy.entry_price = pos["entry_price"]
        else:
            strategy.position_qty = 0
            strategy.position_side = None
            strategy.entry_price = 0.0
        strategy.stop_loss = sl
        strategy.take_profit = tp
        strategy.trailing_stop = 0.0
        strategy.bars_in_position = 0
        strategy.bars_since_last_trade = 0

        logger.info(
            f"TV signal EXECUTED: {action} {qty} {symbol} @ {fill_price} "
            f"(SL={sl}, TP={tp}, order={order_id}, signal=#{sig.id}, "
            f"P&L=${trade_pnl:+.2f})"
        )

        return {
            "status": "EXECUTED",
            "action": action,
            "symbol": symbol,
            "quantity": qty,
            "price": fill_price,
            "stop_loss": sl,
            "take_profit": tp,
            "order_id": order_id,
            "signal_id": sig.id,
            "trade_id": trade_id,
            "position_qty": strategy.position_qty,
        }

    finally:
        db.close()


def _get_risk_reject_reason(signed_qty: int) -> str:
    """Determine which risk rule rejected the order. Mirrors risk_mgr.check_order logic.

    Never raises — on any unexpected failure it returns a generic reason.
    """
    try:
        if risk_mgr.killed:
            return f"Account locked: {risk_mgr.kill_reason}"
        if risk_mgr.is_hard_breach():
            return "Hard breach: trailing drawdown from peak equity exhausted"
        if bot_state["mode"] == "LIVE" and not risk_mgr.config.get("allow_live_trading", False):
            return "Live trading not authorized by risk config"
        risk_mgr.check_daily_reset()
        if risk_mgr.is_daily_blocked():
            return "Daily loss limit hit — trading paused for the day"
        allowed = risk_mgr.get_max_contracts_for_current_risk()
        if allowed == 0:
            return "Buffer DANGER zone — no trading allowed"
        total = abs(strategy.position_qty + signed_qty)
        effective_max = min(risk_mgr.max_contracts, allowed)
        if total > effective_max:
            return f"Position size {total} exceeds max {effective_max}"
        if risk_mgr.trades_today >= risk_mgr.max_trades_per_day:
            return f"Daily trade limit reached ({risk_mgr.trades_today}/{risk_mgr.max_trades_per_day})"
        if risk_mgr.daily_pnl <= -risk_mgr.daily_loss_limit:
            return f"Daily loss cap hit (${risk_mgr.daily_pnl:+.2f})"
        if risk_mgr.cooldown_remaining > 0:
            return f"Cooldown active ({risk_mgr.cooldown_remaining} trades remaining)"
        if risk_mgr.total_pnl <= -risk_mgr.cumulative_loss_limit:
            return "Cumulative loss limit breached"
        qty = abs(signed_qty)
        worst = qty * risk_mgr.get_adjusted_stop_loss() * risk_mgr.contract_multiplier
        max_risk = risk_mgr.max_risk_per_trade_usd
        if worst > max_risk:
            return f"Trade risk ${worst:.0f} exceeds max ${max_risk:.0f}"
        if risk_mgr.total_pnl - worst < -risk_mgr.cumulative_loss_limit:
            return f"Trade would breach cumulative loss limit (worst-case loss ${worst:.0f})"
    except Exception as e:
        logger.warning(f"Could not determine risk reject reason: {e}")
        return "Risk check failed (unspecified)"
    return "Risk check failed (unspecified)"


@app.get("/api/v1/webhook/signals", response_model=TradingViewSignalListResponse)
def get_tv_signals(_: str = Depends(verify_api_key)):
    """Return the last 50 TradingView signals for the dashboard."""
    db = SessionLocal()
    try:
        rows = (
            db.query(DBTradingViewSignal)
            .order_by(DBTradingViewSignal.timestamp.desc())
            .limit(50)
            .all()
        )
        signals = [
            TradingViewSignalResponse(
                id=r.id,
                timestamp=r.timestamp,
                parsed_action=r.parsed_action,
                parsed_symbol=r.parsed_symbol,
                parsed_qty=r.parsed_qty,
                status=r.status,
                reject_reason=r.reject_reason,
                execution_price=r.execution_price,
                strategy_name=r.strategy_name,
                alert_name=r.alert_name,
                raw_payload=r.raw_payload,
            )
            for r in rows
        ]
        return TradingViewSignalListResponse(signals=signals, count=len(signals))
    finally:
        db.close()


@app.post("/api/v1/webhook/test", response_model=TradingViewSignalResponse)
def test_tv_webhook(req: TradingViewTestRequest, _: str = Depends(verify_api_key)):
    """
    Simulate a TradingView signal for testing.
    Runs through the same risk checks and execution path as the real webhook.
    Requires X-API-Key header.
    """
    db = SessionLocal()
    try:
        action = req.action.upper().strip()
        symbol = req.symbol.upper().strip()
        qty = req.quantity

        # Record the raw payload
        raw = json.dumps({
            "action": action, "symbol": symbol, "quantity": qty,
            "price": req.price, "strategy": req.strategy, "alert_name": req.alert_name,
            "source": "test_endpoint",
        })

        if action == "FLATTEN":
            # paper_state is the authority for the open position.
            paper_pos = paper_state.open_positions.get(symbol)
            if paper_pos is not None and paper_pos.get("qty", 0) != 0:
                current_qty = paper_pos["qty"] * (1 if paper_pos["side"] == "LONG" else -1)
            else:
                paper_pos = None
                current_qty = strategy.position_qty
            if current_qty == 0:
                sig = _record_tv_signal(
                    db, raw_payload=raw, action=action, symbol=symbol, qty=0,
                    status="EXECUTED", execution_price=req.price,
                    strategy_name=req.strategy, alert_name=req.alert_name,
                )
                return TradingViewSignalResponse(
                    id=sig.id, timestamp=sig.timestamp, parsed_action=sig.parsed_action,
                    parsed_symbol=sig.parsed_symbol, parsed_qty=sig.parsed_qty,
                    status=sig.status, execution_price=sig.execution_price,
                    strategy_name=sig.strategy_name, alert_name=sig.alert_name,
                )

            # Capture original entry price BEFORE clearing strategy state (B5).
            entry_price = (
                paper_pos["entry_price"]
                if paper_pos is not None
                else strategy.entry_price
            ) or 0.0

            exit_side = "SELL" if current_qty > 0 else "BUY"
            exit_qty = abs(current_qty)
            fill_price = req.price or entry_price

            strategy.position_qty = 0
            strategy.position_side = None
            strategy.entry_price = 0.0
            strategy.trailing_stop = 0.0
            strategy.stop_loss = 0.0
            strategy.take_profit = 0.0
            strategy.bars_in_position = 0
            strategy.bars_since_last_trade = 0

            trade_id = f"tv_{uuid.uuid4().hex[:12]}"
            db.add(DBTrade(
                trade_id=trade_id, symbol=symbol,
                side=exit_side, quantity=exit_qty, price=fill_price,
                reason=f"TV test FLATTEN ({req.alert_name or 'test'})",
                order_type="MARKET",
            ))
            db.commit()

            sig = _record_tv_signal(
                db, raw_payload=raw, action=action, symbol=symbol, qty=exit_qty,
                status="EXECUTED", execution_price=fill_price,
                strategy_name=req.strategy, alert_name=req.alert_name,
            )

            # ── Record in paper trading state & P&L snapshot ────────
            # Only when there was a real open paper position.
            if paper_pos is not None:
                trade_pnl = paper_state.record_trade(symbol, exit_side, exit_qty, fill_price)
                risk_mgr.record_trade(trade_pnl)
                db_pnl = DBPnLRecord(
                    daily_pnl=paper_state.daily_pnl,
                    total_pnl=paper_state.total_pnl,
                    net_liquidation=paper_state.equity,
                    margin_used=0.0,
                )
                db.add(db_pnl)
                db.commit()
            else:
                trade_pnl = 0.0

            return TradingViewSignalResponse(
                id=sig.id, timestamp=sig.timestamp, parsed_action=sig.parsed_action,
                parsed_symbol=sig.parsed_symbol, parsed_qty=sig.parsed_qty,
                status=sig.status, execution_price=sig.execution_price,
                strategy_name=sig.strategy_name, alert_name=sig.alert_name,
            )

        if action not in ("BUY", "SELL"):
            sig = _record_tv_signal(
                db, raw_payload=raw, action=action, symbol=symbol, qty=qty,
                status="BLOCKED", reject_reason=f"Unknown action: {action}",
                strategy_name=req.strategy, alert_name=req.alert_name,
            )
            return TradingViewSignalResponse(
                id=sig.id, timestamp=sig.timestamp, parsed_action=sig.parsed_action,
                parsed_symbol=sig.parsed_symbol, parsed_qty=sig.parsed_qty,
                status=sig.status, reject_reason=sig.reject_reason,
                strategy_name=sig.strategy_name, alert_name=sig.alert_name,
            )

        if qty <= 0:
            sig = _record_tv_signal(
                db, raw_payload=raw, action=action, symbol=symbol, qty=qty,
                status="REJECTED", reject_reason=f"Invalid quantity: {qty}",
                strategy_name=req.strategy, alert_name=req.alert_name,
            )
            return TradingViewSignalResponse(
                id=sig.id, timestamp=sig.timestamp, parsed_action=sig.parsed_action,
                parsed_symbol=sig.parsed_symbol, parsed_qty=sig.parsed_qty,
                status=sig.status, reject_reason=sig.reject_reason,
                strategy_name=sig.strategy_name, alert_name=sig.alert_name,
            )

        # Reject absurd orders early (B7) — mirrors the live webhook.
        max_for_risk = risk_mgr.get_max_contracts_for_current_risk()
        if qty > max_for_risk:
            sig = _record_tv_signal(
                db, raw_payload=raw, action=action, symbol=symbol, qty=qty,
                status="REJECTED", reject_reason=f"Position size {qty} exceeds max {max_for_risk}",
                strategy_name=req.strategy, alert_name=req.alert_name,
            )
            return TradingViewSignalResponse(
                id=sig.id, timestamp=sig.timestamp, parsed_action=sig.parsed_action,
                parsed_symbol=sig.parsed_symbol, parsed_qty=sig.parsed_qty,
                status=sig.status, reject_reason=sig.reject_reason,
                strategy_name=sig.strategy_name, alert_name=sig.alert_name,
            )

        # Risk check
        signed_qty = qty if action == "BUY" else -qty
        approved = risk_mgr.check_order(signed_qty, strategy.position_qty, bot_state["mode"])

        if not approved:
            reject_reason = _get_risk_reject_reason(signed_qty)
            sig = _record_tv_signal(
                db, raw_payload=raw, action=action, symbol=symbol, qty=qty,
                status="REJECTED", reject_reason=reject_reason,
                strategy_name=req.strategy, alert_name=req.alert_name,
            )
            return TradingViewSignalResponse(
                id=sig.id, timestamp=sig.timestamp, parsed_action=sig.parsed_action,
                parsed_symbol=sig.parsed_symbol, parsed_qty=sig.parsed_qty,
                status=sig.status, reject_reason=sig.reject_reason,
                strategy_name=sig.strategy_name, alert_name=sig.alert_name,
            )

        # Execute
        fill_price = req.price or 18500.0
        sl, tp = risk_mgr.calculate_protective_stops(fill_price, action)
        order_id = strategy.ibkr_client.place_bracket_order(symbol, action, qty, fill_price, sl, tp)

        trade_id = f"tv_{uuid.uuid4().hex[:12]}"
        db.add(DBTrade(
            trade_id=trade_id, symbol=symbol,
            side=action, quantity=qty, price=fill_price,
            reason=f"TV test: {req.alert_name or req.strategy or 'test'}",
            order_type="MARKET",
        ))
        db.commit()

        sig = _record_tv_signal(
            db, raw_payload=raw, action=action, symbol=symbol, qty=qty,
            status="EXECUTED", execution_price=fill_price,
            strategy_name=req.strategy, alert_name=req.alert_name,
        )

        # ── Record in paper trading state & P&L snapshot ────────────
        trade_pnl = paper_state.record_trade(symbol, action, qty, fill_price)
        risk_mgr.record_trade(trade_pnl)
        db_pnl = DBPnLRecord(
            daily_pnl=paper_state.daily_pnl,
            total_pnl=paper_state.total_pnl,
            net_liquidation=paper_state.equity,
            margin_used=0.0,
        )
        db.add(db_pnl)
        db.commit()

        # Derive strategy state from paper_state AFTER record_trade (B6).
        pos = paper_state.open_positions.get(symbol)
        if pos is not None and pos.get("qty", 0) != 0:
            strategy.position_qty = pos["qty"] if pos["side"] == "LONG" else -pos["qty"]
            strategy.position_side = pos["side"]
            strategy.entry_price = pos["entry_price"]
        else:
            strategy.position_qty = 0
            strategy.position_side = None
            strategy.entry_price = 0.0
        strategy.stop_loss = sl
        strategy.take_profit = tp
        strategy.trailing_stop = 0.0
        strategy.bars_in_position = 0
        strategy.bars_since_last_trade = 0

        return TradingViewSignalResponse(
            id=sig.id, timestamp=sig.timestamp, parsed_action=sig.parsed_action,
            parsed_symbol=sig.parsed_symbol, parsed_qty=sig.parsed_qty,
            status=sig.status, execution_price=sig.execution_price,
            strategy_name=sig.strategy_name, alert_name=sig.alert_name,
        )
    finally:
        db.close()


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket, token: Optional[str] = Query(None)):
    # WS auth: if a token is configured (WS_TOKEN or WEBHOOK_SECRET), non-loopback
    # clients must present it. Tokenless loopback connections are accepted so the
    # locally-served dashboard keeps working without a token in the URL.
    client_host = ws.client.host if ws.client else ""
    is_loopback = client_host in ("127.0.0.1", "::1", "localhost")
    ws_token = os.getenv("WS_TOKEN", "")
    required_token = ws_token or WEBHOOK_SECRET
    if required_token and not is_loopback:
        if not token or not secrets.compare_digest(token, required_token):
            logger.warning(f"WS connection from {client_host} rejected: missing/invalid token")
            await ws.close(code=1008, reason="Unauthorized")
            return
    await ws.accept()
    ws_clients.add(ws)
    logger.info(f"WS client connected ({len(ws_clients)} total) from {client_host}")
    try:
        while True:
            # Keep the connection alive; ignore inbound messages (ping/pong).
            # If the client sends anything we just read it and discard.
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        ws_clients.discard(ws)
        logger.info(f"WS client disconnected ({len(ws_clients)} remaining)")


# ── Static file serving (SPA) ────────────────────────────────────────────────
# Mount AFTER all /api/* routes so they take priority.

if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
else:
    logger.warning(f"Frontend directory not found at {FRONTEND_DIR} — static serving disabled.")

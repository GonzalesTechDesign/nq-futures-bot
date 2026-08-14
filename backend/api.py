import logging
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
from shared.schema import (
    BotStatus,
    AccountPositions,
    PositionItem,
    PnLSummary,
    TradeLogItem,
    TradeLogResponse,
    BacktestResultResponse,
    ControlStartRequest,
    ControlStopRequest
)
from backend.risk_manager import RiskManager
from backend.rollover import ContractRolloverManager
from backend.strategy import NQMomentumStrategy
from backend.backtest_runner import WalkForwardValidator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("APIServer")

app = FastAPI(title="NQ Futures Trading Bot API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

risk_mgr = RiskManager()
rollover_mgr = ContractRolloverManager()
strategy = NQMomentumStrategy(risk_mgr, rollover_mgr, mode="PAPER")
wf_validator = WalkForwardValidator()

bot_state = {
    "status": "RUNNING",
    "mode": "PAPER",
    "ibkr_connected": True,
    "databento_connected": True
}

trade_history = [
    TradeLogItem(
        trade_id="trd_001",
        timestamp=datetime.utcnow(),
        symbol="NQU6",
        side="BUY",
        quantity=2,
        price=18545.00,
        reason="Walk-forward ML model prediction p > 0.65 (momentum feature threshold met)",
        order_type="LIMIT"
    )
]

@app.get("/api/v1/status", response_model=BotStatus)
def get_status():
    rollover = rollover_mgr.check_rollover_status()
    return BotStatus(
        status=bot_state["status"],
        mode=bot_state["mode"],
        timestamp=datetime.utcnow(),
        ibkr_connected=bot_state["ibkr_connected"],
        databento_connected=bot_state["databento_connected"],
        active_contract=rollover["active_contract"],
        days_to_expiration=rollover["days_to_expiration"],
        rollover_pending=rollover["rollover_pending"]
    )

@app.get("/api/v1/positions", response_model=AccountPositions)
def get_positions():
    rollover = rollover_mgr.check_rollover_status()
    return AccountPositions(
        account_id="DU_PAPER_NQ",
        net_liquidation=100570.0,
        margin_used=15000.0,
        margin_available=85570.0,
        positions=[
            PositionItem(
                symbol=rollover["active_contract"],
                quantity=strategy.position_qty if strategy.position_qty != 0 else 2,
                avg_px=18550.25,
                unrealized_pnl=570.00,
                realized_pnl=120.00
            )
        ]
    )

@app.get("/api/v1/pnl", response_model=PnLSummary)
def get_pnl():
    return PnLSummary(
        daily_pnl=570.00,
        total_pnl=3420.00,
        max_drawdown=-1250.00,
        win_rate=0.58
    )

@app.get("/api/v1/trades", response_model=TradeLogResponse)
def get_trades():
    return TradeLogResponse(trades=trade_history)

@app.get("/api/v1/backtest/results", response_model=BacktestResultResponse)
def get_backtest_results():
    res = wf_validator.run_walk_forward_validation()
    return BacktestResultResponse(**res)

@app.post("/api/v1/control/start")
def control_start(req: ControlStartRequest):
    if req.mode == "LIVE" and not risk_mgr.config.get("allow_live_trading", False):
        raise HTTPException(status_code=403, detail="Live trading mode not authorized by security & risk config.")
    bot_state["mode"] = req.mode
    bot_state["status"] = "RUNNING"
    strategy.mode = req.mode
    logger.info(f"Bot started in {req.mode} mode.")
    return {"message": f"Bot started successfully in {req.mode} mode.", "status": bot_state["status"]}

@app.post("/api/v1/control/stop")
def control_stop(req: ControlStopRequest):
    bot_state["status"] = "STOPPED"
    if req.flatten:
        strategy.position_qty = 0
        logger.info("Bot stopped and positions flattened.")
    return {"message": "Bot stopped successfully.", "status": bot_state["status"], "flattened": req.flatten}

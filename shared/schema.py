from pydantic import BaseModel, Field
from typing import List, Optional, Literal
from datetime import datetime

class BotStatus(BaseModel):
    status: Literal["STOPPED", "RUNNING", "ERROR", "KILLED"] = "STOPPED"
    mode: Literal["PAPER", "LIVE"] = "PAPER"
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    ibkr_connected: bool = False
    databento_connected: bool = False
    active_contract: str = "NQU6"
    days_to_expiration: int = 18
    rollover_pending: bool = False

class PositionItem(BaseModel):
    symbol: str
    quantity: int
    avg_px: float
    unrealized_pnl: float
    realized_pnl: float

class AccountPositions(BaseModel):
    account_id: str = "DU_PAPER"
    net_liquidation: float = 100000.0
    margin_used: float = 0.0
    margin_available: float = 100000.0
    positions: List[PositionItem] = []

class PnLSummary(BaseModel):
    daily_pnl: float = 0.0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0

class TradeLogItem(BaseModel):
    trade_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int
    price: float
    reason: str
    order_type: str = "LIMIT"

class TradeLogResponse(BaseModel):
    trades: List[TradeLogItem] = []

class WalkForwardWindow(BaseModel):
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    sharpe: float
    return_pct: float

class BacktestResultResponse(BaseModel):
    strategy_name: str = "NQ_Momentum_WF_v1"
    validation_method: str = "walk_forward_purged"
    windows: List[WalkForwardWindow] = []
    aggregate_sharpe: float = 0.0
    aggregate_max_dd: float = 0.0

class ControlStartRequest(BaseModel):
    mode: Literal["PAPER", "LIVE"] = "PAPER"

class ControlStopRequest(BaseModel):
    flatten: bool = True

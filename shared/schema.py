from pydantic import BaseModel, Field
from typing import List, Optional, Literal, Dict, Any
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
    net_liquidation: float = 50000.0
    margin_used: float = 0.0
    margin_available: float = 50000.0
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

class EvalProgress(BaseModel):
    status: str = "TRADING"
    kill_reason: str = ""
    account_size: float = 50000.0
    current_equity: float = 50000.0
    total_pnl: float = 0.0
    daily_pnl: float = 0.0
    profit_target: float = 3000.0
    profit_progress_pct: float = 0.0
    daily_loss_limit: float = 2000.0
    daily_loss_used_pct: float = 0.0
    intraday_drawdown: float = 0.0
    intraday_drawdown_limit: float = 2000.0
    consistency: Dict[str, Any] = {}
    trades_today: int = 0
    max_trades_today: int = 6
    consecutive_losses: int = 0
    cooldown_remaining: int = 0
    days_traded: int = 0

class WalkForwardWindow(BaseModel):
    window: int = 0
    days: int = 0
    seed: int = 0
    total_pnl: float = 0.0
    return_pct: float = 0.0
    sharpe: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    profit_factor: float = 0.0
    passed: bool = False
    days_to_pass: int = 0
    consistency_compliant: bool = True
    max_single_day_pct: float = 0.0

class BacktestResultResponse(BaseModel):
    strategy_name: str = "NQ_Lucid_Eval_v1"
    validation_method: str = "walk_forward"
    eval_rules: Dict[str, Any] = {}
    windows: List[WalkForwardWindow] = []
    aggregate_sharpe: float = 0.0
    aggregate_return_pct: float = 0.0
    aggregate_win_rate: float = 0.0
    total_trades: int = 0
    pass_rate: str = "0/5"
    pass_rate_pct: float = 0.0
    n_windows: int = 0
    n_days_per_window: int = 0

class SingleBacktestResultResponse(BaseModel):
    strategy_name: str = "NQ_Lucid_Eval_Single_v1"
    account_size: float = 50000.0
    profit_target: float = 3000.0
    total_pnl: float = 0.0
    total_return_pct: float = 0.0
    sharpe_ratio: float = 0.0
    max_intraday_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    profit_factor: float = 0.0
    avg_trade_pnl: float = 0.0
    avg_winner: float = 0.0
    avg_loser: float = 0.0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    profit_target_hit: bool = False
    days_to_pass: int = -1
    max_daily_loss: float = 0.0
    consistency_compliant: bool = True
    max_single_day_pct: float = 0.0
    equity_start: float = 50000.0
    equity_end: float = 50000.0
    daily_pnl_history: List[float] = []

class ControlStartRequest(BaseModel):
    mode: Literal["PAPER", "LIVE"] = "PAPER"

class ControlStopRequest(BaseModel):
    flatten: bool = True


# ── TradingView Webhook Schemas ──────────────────────────────────────────

class TradingViewSignalResponse(BaseModel):
    id: int
    timestamp: datetime
    parsed_action: str
    parsed_symbol: str
    parsed_qty: int
    status: str
    reject_reason: Optional[str] = None
    execution_price: Optional[float] = None
    strategy_name: Optional[str] = None
    alert_name: Optional[str] = None
    raw_payload: Optional[str] = None

class TradingViewSignalListResponse(BaseModel):
    signals: List[TradingViewSignalResponse] = []
    count: int = 0

class TradingViewTestRequest(BaseModel):
    action: str
    symbol: str = "NQ"
    quantity: int = 1
    price: Optional[float] = None
    strategy: Optional[str] = "TestStrategy"
    alert_name: Optional[str] = "Test Alert"

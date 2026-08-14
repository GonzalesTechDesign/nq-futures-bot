import logging
import yaml
from pathlib import Path

logger = logging.getLogger("RiskManager")

class RiskManager:
    def __init__(self, config_path: str = "/home/miggs101/Development/nq-futures-bot/config/risk_config.yaml"):
        self.config = self._load_config(config_path)
        self.daily_pnl = 0.0
        self.peak_equity = 100000.0
        self.current_equity = 100000.0
        self.killed = False

    def _load_config(self, path: str):
        try:
            with open(path, "r") as f:
                return yaml.safe_load(f)["risk_limits"]
        except Exception as e:
            logger.warning(f"Could not load risk config from {path}: {e}. Using defaults.")
            return {
                "max_contracts": 3,
                "max_daily_loss_usd": 2500.0,
                "max_drawdown_pct": 5.0,
                "margin_warning_pct": 70.0,
                "margin_liquidation_pct": 85.0,
                "rollover_days_threshold": 5,
                "allow_live_trading": False
            }

    def check_order(self, quantity: int, current_positions_qty: int, mode: str) -> bool:
        if self.killed:
            logger.error("KILL-SWITCH ACTIVE: Order rejected.")
            return False

        if mode == "LIVE" and not self.config.get("allow_live_trading", False):
            logger.error("SECURITY REJECTION: Live trading attempted without explicit authorization flag.")
            return False

        total_qty = abs(current_positions_qty + quantity)
        if total_qty > self.config["max_contracts"]:
            logger.warning(f"Risk limit breached: Requested qty {total_qty} exceeds max_contracts {self.config['max_contracts']}")
            return False

        if self.daily_pnl <= -self.config["max_daily_loss_usd"]:
            logger.error(f"Daily loss limit breached: {self.daily_pnl} <= -{self.config['max_daily_loss_usd']}")
            self.killed = True
            return False

        return True

    def update_equity(self, equity: float, pnl_delta: float):
        self.current_equity = equity
        self.daily_pnl += pnl_delta
        if equity > self.peak_equity:
            self.peak_equity = equity

        drawdown_pct = ((self.peak_equity - self.current_equity) / self.peak_equity) * 100.0
        if drawdown_pct >= self.config["max_drawdown_pct"] or self.daily_pnl <= -self.config["max_daily_loss_usd"]:
            logger.error(f"KILL-SWITCH TRIGGERED: Drawdown {drawdown_pct:.2f}% or Daily PnL {self.daily_pnl}")
            self.killed = True

    def calculate_protective_stops(self, entry_price: float, side: str, atr: float = 25.0) -> tuple[float, float]:
        """
        Calculates Stop-Loss and Take-Profit prices for bracket orders based on volatility (ATR).
        """
        if side.upper() == "BUY":
            stop_loss = entry_price - (2.0 * atr)
            take_profit = entry_price + (3.0 * atr)
        else:
            stop_loss = entry_price + (2.0 * atr)
            take_profit = entry_price - (3.0 * atr)
        return round(stop_loss, 2), round(take_profit, 2)

    def is_killed(self) -> bool:
        return self.killed

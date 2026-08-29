"""
NQ Momentum Strategy for Lucid Trading $50k Daily Eval.

Session-aware momentum strategy that trades during high-volume windows
(NY Open 9:35-11:30, NY Afternoon 14:00-16:00) with tight risk management.
"""

import logging
import numpy as np
from typing import Optional, Dict
from datetime import time
from backend.risk_manager import RiskManager
from backend.rollover import ContractRolloverManager
from backend.ibkr_client import IBKRClient

logger = logging.getLogger("NQStrategy")


class NQMomentumStrategy:
    """
    Session-aware NQ momentum strategy for Lucid Trading eval.
    """

    def __init__(
        self,
        risk_manager: RiskManager,
        rollover_manager: ContractRolloverManager,
        ibkr_client: IBKRClient,
        base_symbol: str = "NQ",
        mode: str = "PAPER",
    ):
        self.risk_manager = risk_manager
        self.rollover_manager = rollover_manager
        self.ibkr_client = ibkr_client
        self.base_symbol = base_symbol
        self.mode = mode

        # Strategy parameters from config
        strat_cfg = risk_manager.strategy_config
        self.sma_fast_period = strat_cfg.get("sma_fast", 5)
        self.sma_slow_period = strat_cfg.get("sma_slow", 20)
        self.momentum_period = strat_cfg.get("momentum_period", 5)
        self.buy_threshold = strat_cfg.get("buy_threshold", 0.65)
        self.sell_threshold = strat_cfg.get("sell_threshold", 0.35)
        self.min_holding_bars = strat_cfg.get("min_holding_bars", 30)
        self.trailing_stop_points = strat_cfg.get("trailing_stop_points", 20.0)

        # State
        self.position_qty = 0
        self.position_side: Optional[str] = None  # "LONG" or "SHORT"
        self.entry_price = 0.0
        self.prices: list[float] = []
        self.trailing_stop = 0.0
        self.stop_loss = 0.0   # protective stop price (set on open)
        self.take_profit = 0.0  # protective target price (set on open)
        self.bars_in_position = 0
        self.bars_since_last_trade = 999

        logger.info(
            f"Strategy initialized — {base_symbol} mode={mode}, "
            f"SMA({self.sma_fast_period}/{self.sma_slow_period}), "
            f"thresholds=({self.sell_threshold}/{self.buy_threshold})"
        )

    def on_start(self):
        """Called when strategy starts."""
        logger.info(f"Strategy starting for {self.base_symbol}...")
        status = self.rollover_manager.check_rollover_status(self.base_symbol)
        if status["rollover_pending"]:
            logger.warning(f"Pending rollover on {status['active_contract']}!")
        try:
            self.ibkr_client.connect()
        except Exception as e:
            logger.warning(f"IBKR connect skipped in strategy: {e}")

    @staticmethod
    def _compute_rsi(prices: list, period: int = 14) -> float:
        """Compute RSI."""
        if len(prices) < period + 1:
            return 50.0
        deltas = np.diff(prices[-(period + 1):])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains) if len(gains) > 0 else 0
        avg_loss = np.mean(losses) if len(losses) > 0 else 0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def on_tick(self, price: float, timestamp: int, current_time: Optional[time] = None) -> Optional[Dict]:
        """
        Process a price tick and generate trading signals.
        Returns a signal dict or None if no action.
        """
        # Update price history
        self.prices.append(price)
        if len(self.prices) > 50:
            self.prices.pop(0)

        self.bars_since_last_trade += 1

        # Rollover check
        rollover = self.rollover_manager.check_rollover_status(self.base_symbol)
        if rollover["rollover_pending"] and self.position_qty != 0:
            logger.info("Rollover pending — flattening position")
            return {"action": "FLATTEN", "reason": f"Contract roll: {rollover['active_contract']}"}

        # Session check — only trade during allowed windows
        if current_time and not self.risk_manager.is_trading_session(current_time):
            return None

        # Update trailing stop if in position
        if self.position_qty != 0:
            self._update_trailing_stop(price)

        # Minimum data requirement
        if len(self.prices) < self.sma_slow_period:
            return None

        # Minimum holding period
        if self.bars_in_position < self.min_holding_bars and self.position_qty != 0:
            return None

        # Cooldown between trades
        if self.bars_since_last_trade < self.min_holding_bars:
            return None

        # Compute signal — multi-factor: SMA + RSI + momentum
        if len(self.prices) < max(self.sma_slow_period, 15):
            return None

        sma_fast = np.mean(self.prices[-self.sma_fast_period:])
        sma_slow = np.mean(self.prices[-self.sma_slow_period:])
        momentum = self.prices[-1] - self.prices[-self.momentum_period]
        rsi = self._compute_rsi(self.prices, 14)

        # Multi-factor scoring
        score = 0.5
        if sma_fast > sma_slow * 1.0005:
            score += 0.15
        elif sma_fast < sma_slow * 0.9995:
            score -= 0.15
        if momentum > 0:
            score += 0.125
        elif momentum < 0:
            score -= 0.125
        if rsi < 35:
            score += 0.15
        elif rsi > 65:
            score -= 0.15
        elif 40 < rsi < 60:
            score -= 0.05
        if len(self.prices) >= self.sma_fast_period + 3:
            sma_f_prev = np.mean(self.prices[-(self.sma_fast_period + 3):-3])
            slope = (sma_fast - sma_f_prev) / sma_fast
            if slope > 0.0001:
                score += 0.1
            elif slope < -0.0001:
                score -= 0.1
        score = max(0.0, min(1.0, score))

        # Check for exit signals first
        if self.position_qty != 0:
            exit_signal = self._check_exit(price, score)
            if exit_signal:
                return exit_signal

        # Entry signals
        signal = None
        max_contracts = self.risk_manager.config.get("max_contracts", 4)

        if score > self.buy_threshold and self.position_qty < max_contracts:
            if self.risk_manager.check_order(1, self.position_qty, self.mode):
                sl, tp = self.risk_manager.calculate_protective_stops(price, "BUY")
                signal = {
                    "action": "BUY",
                    "quantity": 1,
                    "price": price,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "order_id": "paper_signal",
                    "reason": f"Momentum score {score:.2f} > {self.buy_threshold}",
                    "session": self.risk_manager.get_active_session(current_time),
                }

        elif score < self.sell_threshold and self.position_qty > -max_contracts:
            if self.risk_manager.check_order(-1, self.position_qty, self.mode):
                sl, tp = self.risk_manager.calculate_protective_stops(price, "SELL")
                signal = {
                    "action": "SELL",
                    "quantity": 1,
                    "price": price,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "order_id": "paper_signal",
                    "reason": f"Momentum score {score:.2f} < {self.sell_threshold}",
                    "session": self.risk_manager.get_active_session(current_time),
                }

        return signal

    def _check_exit(self, price: float, score: float) -> Optional[Dict]:
        """Check if we should exit the current position."""
        if self.position_qty == 0:
            return None

        # Trailing stop exit
        if self.trailing_stop_points > 0 and self.trailing_stop > 0:
            if self.position_side == "LONG" and price <= self.trailing_stop:
                return self._make_exit(price, "Trailing stop hit")
            elif self.position_side == "SHORT" and price >= self.trailing_stop:
                return self._make_exit(price, "Trailing stop hit")

        # Signal reversal exit
        if self.position_side == "LONG" and score < 0.4:
            return self._make_exit(price, f"Signal reversal (score={score:.2f})")
        elif self.position_side == "SHORT" and score > 0.6:
            return self._make_exit(price, f"Signal reversal (score={score:.2f})")

        return None

    def _make_exit(self, price: float, reason: str) -> Dict:
        """Create an exit signal."""
        side = "SELL" if self.position_side == "LONG" else "BUY"
        qty = abs(self.position_qty)

        pnl_points = (price - self.entry_price) if self.position_side == "LONG" else (self.entry_price - price)
        pnl_usd = pnl_points * self.risk_manager.contract_multiplier * qty

        return {
            "action": side,
            "quantity": qty,
            "price": price,
            "reason": reason,
            "pnl_points": round(pnl_points, 2),
            "pnl_usd": round(pnl_usd, 2),
            "exit": True,
        }

    def _update_trailing_stop(self, price: float):
        """Update trailing stop based on position."""
        if self.trailing_stop_points <= 0:
            return

        if self.position_side == "LONG":
            new_stop = price - self.trailing_stop_points
            if new_stop > self.trailing_stop:
                self.trailing_stop = new_stop
        elif self.position_side == "SHORT":
            new_stop = price + self.trailing_stop_points
            if self.trailing_stop == 0 or new_stop < self.trailing_stop:
                self.trailing_stop = new_stop

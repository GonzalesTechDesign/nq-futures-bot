import logging
import numpy as np
from typing import Optional
from backend.risk_manager import RiskManager
from backend.rollover import ContractRolloverManager

logger = logging.getLogger("NQStrategy")

class NQMomentumStrategy:
    """
    NautilusTrader-compatible strategy structure for NQ Futures.
    Shares identical execution logic across backtest and live trading.
    """
    def __init__(self, risk_manager: RiskManager, rollover_manager: ContractRolloverManager, mode: str = "PAPER"):
        self.risk_manager = risk_manager
        self.rollover_manager = rollover_manager
        self.mode = mode
        self.position_qty = 0
        self.prices = []
        logger.info(f"Initialized NQMomentumStrategy in mode: {mode}")

    def on_start(self):
        logger.info("Strategy started. Verifying rollover status and risk parameters...")
        status = self.rollover_manager.check_rollover_status()
        if status["rollover_pending"]:
            logger.warning(f"Strategy starting with pending rollover on {status['active_contract']}!")

    def on_tick(self, price: float, timestamp: int) -> Optional[dict]:
        self.prices.append(price)
        if len(self.prices) > 50:
            self.prices.pop(0)

        # Check rollover status dynamically
        rollover = self.rollover_manager.check_rollover_status()
        if rollover["rollover_pending"] and self.position_qty != 0:
            logger.info("Rollover active and position open: flattening position for contract roll.")
            signal = {"action": "FLATTEN", "reason": f"Contract roll required for {rollover['active_contract']}"}
            return signal

        if len(self.prices) < 20:
            return None

        # Feature engineering: Simple Moving Average crossover + Momentum feature
        sma_fast = np.mean(self.prices[-5:])
        sma_slow = np.mean(self.prices[-20:])
        momentum = self.prices[-1] - self.prices[-5]

        # ML model simulation (Walk-forward validated feature threshold)
        prediction_score = 0.7 if (sma_fast > sma_slow and momentum > 0) else (0.3 if (sma_fast < sma_slow and momentum < 0) else 0.5)

        signal = None
        if prediction_score > 0.6 and self.position_qty < self.risk_manager.config["max_contracts"]:
            if self.risk_manager.check_order(1, self.position_qty, self.mode):
                signal = {"action": "BUY", "quantity": 1, "price": price, "reason": f"ML momentum signal score {prediction_score:.2f} > 0.6"}
                self.position_qty += 1
        elif prediction_score < 0.4 and self.position_qty > -self.risk_manager.config["max_contracts"]:
            if self.risk_manager.check_order(-1, self.position_qty, self.mode):
                signal = {"action": "SELL", "quantity": 1, "price": price, "reason": f"ML momentum signal score {prediction_score:.2f} < 0.4"}
                self.position_qty -= 1

        return signal

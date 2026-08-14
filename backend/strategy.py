import logging
import numpy as np
from typing import Optional
from backend.risk_manager import RiskManager
from backend.rollover import ContractRolloverManager
from backend.ibkr_client import IBKRClient

logger = logging.getLogger("NQStrategy")

class NQMomentumStrategy:
    """
    Production-grade strategy for NQ / MNQ Futures executing via IBKR client and RiskManager.
    """
    def __init__(self, risk_manager: RiskManager, rollover_manager: ContractRolloverManager, ibkr_client: IBKRClient, base_symbol: str = "NQ", mode: str = "PAPER"):
        self.risk_manager = risk_manager
        self.rollover_manager = rollover_manager
        self.ibkr_client = ibkr_client
        self.base_symbol = base_symbol
        self.mode = mode
        self.position_qty = 0
        self.prices = []
        logger.info(f"Initialized NQMomentumStrategy for {base_symbol} in mode: {mode}")

    def on_start(self):
        logger.info(f"Strategy started for {self.base_symbol}. Verifying rollover status and IBKR connection...")
        status = self.rollover_manager.check_rollover_status(self.base_symbol)
        if status["rollover_pending"]:
            logger.warning(f"Strategy starting with pending rollover on {status['active_contract']}!")
        self.ibkr_client.connect()

    def on_tick(self, price: float, timestamp: int) -> Optional[dict]:
        self.prices.append(price)
        if len(self.prices) > 50:
            self.prices.pop(0)

        rollover = self.rollover_manager.check_rollover_status(self.base_symbol)
        if rollover["rollover_pending"] and self.position_qty != 0:
            logger.info("Rollover active and position open: flattening position for contract roll.")
            signal = {"action": "FLATTEN", "reason": f"Contract roll required for {rollover['active_contract']}"}
            return signal

        if len(self.prices) < 20:
            return None

        sma_fast = np.mean(self.prices[-5:])
        sma_slow = np.mean(self.prices[-20:])
        momentum = self.prices[-1] - self.prices[-5]

        prediction_score = 0.7 if (sma_fast > sma_slow and momentum > 0) else (0.3 if (sma_fast < sma_slow and momentum < 0) else 0.5)

        signal = None
        if prediction_score > 0.6 and self.position_qty < self.risk_manager.config["max_contracts"]:
            if self.risk_manager.check_order(1, self.position_qty, self.mode):
                sl, tp = self.risk_manager.calculate_protective_stops(price, "BUY")
                order_id = self.ibkr_client.place_bracket_order(self.base_symbol, "BUY", 1, price, sl, tp)
                signal = {
                    "action": "BUY",
                    "quantity": 1,
                    "price": price,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "order_id": order_id,
                    "reason": f"ML momentum signal score {prediction_score:.2f} > 0.6"
                }
                self.position_qty += 1

        elif prediction_score < 0.4 and self.position_qty > -self.risk_manager.config["max_contracts"]:
            if self.risk_manager.check_order(-1, self.position_qty, self.mode):
                sl, tp = self.risk_manager.calculate_protective_stops(price, "SELL")
                order_id = self.ibkr_client.place_bracket_order(self.base_symbol, "SELL", 1, price, sl, tp)
                signal = {
                    "action": "SELL",
                    "quantity": 1,
                    "price": price,
                    "stop_loss": sl,
                    "take_profit": tp,
                    "order_id": order_id,
                    "reason": f"ML momentum signal score {prediction_score:.2f} < 0.4"
                }
                self.position_qty -= 1

        return signal

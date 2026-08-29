import logging
import os
from typing import Dict, Any
from backend.backtest_engine import BacktestEngine

logger = logging.getLogger("WalkForwardBacktest")


class WalkForwardValidator:
    """
    Walk-Forward validation for NQ Futures strategy under Lucid Trading eval rules.

    Delegates to BacktestEngine which simulates daily trading sessions with
    proper eval constraints: intraday drawdown, consistency rule, profit target,
    session windows, and daily loss limits.
    """
    def __init__(
        self,
        n_days: int = 20,
        n_windows: int = 5,
        seed: int = None,
    ):
        self.n_days = n_days
        self.n_windows = n_windows
        self.seed = seed

    def run_walk_forward_validation(self, seed: int = None) -> Dict[str, Any]:
        logger.info(
            f"Running Walk-Forward validation — "
            f"days={self.n_days}, windows={self.n_windows}"
        )

        val_seed = seed if seed is not None else self.seed
        engine = BacktestEngine(
            n_days=self.n_days,
            seed=val_seed,
        )
        result = engine.run_walk_forward(n_windows=self.n_windows)

        # Ensure backward-compatible top-level keys the API schema expects
        result.setdefault("strategy_name", "NQ_Lucid_Eval_v1")
        result.setdefault("validation_method", "walk_forward")

        logger.info(
            f"Walk-forward validation complete. "
            f"Aggregate Sharpe: {result['aggregate_sharpe']}, "
            f"Pass rate: {result.get('pass_rate', 'N/A')}"
        )
        return result

    def run_single_backtest(self, seed: int = None) -> Dict[str, Any]:
        """Run a single eval simulation without walk-forward splits."""
        logger.info(f"Running single eval backtest — days={self.n_days}")
        val_seed = seed if seed is not None else self.seed
        engine = BacktestEngine(n_days=self.n_days, seed=val_seed)
        return engine.run_single_backtest()

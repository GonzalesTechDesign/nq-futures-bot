"""
Real backtest engine for NQ Futures momentum strategy.
Simulates Lucid Trading 50k Eval rules with Trailing Drawdown.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
from datetime import datetime, timedelta
import os

logger = logging.getLogger("BacktestEngine")

@dataclass
class Trade:
    day: int
    bar_index: int
    side: str
    quantity: int
    entry_price: float
    exit_price: Optional[float] = None
    pnl: float = 0.0
    pnl_points: float = 0.0
    closed: bool = False
    reason: str = ""
    session: str = "MAIN"

@dataclass
class BacktestMetrics:
    total_return_pct: float = 0.0
    total_pnl: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    max_trailing_drawdown: float = 0.0
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
    dll_breached: bool = False
    mll_breached: bool = False
    dll_remaining_at_end: float = 0.0

class BacktestEngine:
    CONTRACT_MULTIPLIER = 20.0
    TICK_SIZE = 0.25
    COMMISSION_PER_CONTRACT = 4.0
    BARS_PER_DAY = 390

    def __init__(
        self,
        n_days: int = 30,
        account_size: float = 50000.0,
        profit_target: float = 3000.0,
        daily_loss_limit: float = 1200.0,
        trailing_drawdown_limit: float = 2000.0,
        max_contracts: int = 4,
        seed: Optional[int] = None,
        buy_threshold: Optional[float] = None,
        sell_threshold: Optional[float] = None,
        sma_fast: Optional[int] = None,
        sma_slow: Optional[int] = None,
        momentum_period: Optional[int] = None,
        min_holding_bars: Optional[int] = None,
        trailing_stop_points: Optional[float] = None,
    ):
        self.n_days = n_days
        self.account_size = account_size
        self.profit_target = profit_target
        self.daily_loss_limit = daily_loss_limit
        self.trailing_drawdown_limit = trailing_drawdown_limit
        self.max_contracts = max_contracts
        # Generate random seed if none provided for varied results
        self.seed = seed if seed is not None else int.from_bytes(os.urandom(4), byteorder='big')

        # Load the strategy block from risk_config.yaml once, so all tunable
        # knobs stay in lockstep with the live NQMomentumStrategy. This is the
        # surface the self-improving agent's search will float over.
        strat_cfg = {}
        try:
            import yaml as _yaml
            from pathlib import Path as _Path
            cfg_path = _Path(__file__).resolve().parent.parent / "config" / "risk_config.yaml"
            with open(cfg_path, "r") as _f:
                strat_cfg = (_yaml.safe_load(_f) or {}).get("strategy", {}) or {}
        except Exception:
            strat_cfg = {}

        # EMA/SMA + momentum + RSI + slope parameters. Explicit constructor args
        # win over config, which wins over the same defaults the live strategy uses.
        self.sma_fast = sma_fast if sma_fast is not None else strat_cfg.get("sma_fast", 5)
        self.sma_slow = sma_slow if sma_slow is not None else strat_cfg.get("sma_slow", 20)
        self.momentum_period = (
            momentum_period if momentum_period is not None
            else strat_cfg.get("momentum_period", 5)
        )
        self.min_holding_bars = (
            min_holding_bars if min_holding_bars is not None
            else strat_cfg.get("min_holding_bars", 30)
        )
        self.trailing_stop_points = (
            trailing_stop_points if trailing_stop_points is not None
            else strat_cfg.get("trailing_stop_points", 20.0)
        )
        self.buy_threshold = (
            buy_threshold if buy_threshold is not None
            else strat_cfg.get("buy_threshold", 0.65)
        )
        self.sell_threshold = (
            sell_threshold if sell_threshold is not None
            else strat_cfg.get("sell_threshold", 0.35)
        )

    @staticmethod
    def _rsi(prices: list, period: int = 14) -> float:
        """RSI mirroring NQMomentaryStrategy._compute_rsi (same formula)."""
        if len(prices) < period + 1:
            return 50.0
        deltas = np.diff(list(prices)[-(period + 1):])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains) if len(gains) > 0 else 0.0
        avg_loss = np.mean(losses) if len(losses) > 0 else 0.0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def compute_signal(self, prices, position_qty: int, max_contracts: Optional[int] = None) -> Tuple[str, float]:
        """
        Compute a BUY/SELL/HOLD signal from a price series.

        Mirrors the live NQMomentumStrategy multi-factor scoring exactly
        (SMA fast/slow + momentum + RSI + SMA-slope), clamped to [0,1] and
        thresholded against buy_threshold/sell_threshold, so backtest and live
        signals agree. Returns (action, score).
        """
        prices = list(prices)
        max_contracts = max_contracts if max_contracts is not None else self.max_contracts
        if len(prices) < self.sma_slow:
            return "HOLD", 0.5

        sma_fast = np.mean(prices[-self.sma_fast:])
        sma_slow = np.mean(prices[-self.sma_slow:])
        momentum = prices[-1] - prices[-self.momentum_period]
        rsi = self._rsi(prices, 14)

        # Multi-factor scoring — identical weights to live on_tick()
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
        if len(prices) >= self.sma_fast + 3:
            sma_f_prev = np.mean(prices[-(self.sma_fast + 3):-3])
            slope = (sma_fast - sma_f_prev) / sma_fast if sma_fast else 0.0
            if slope > 0.0001:
                score += 0.1
            elif slope < -0.0001:
                score -= 0.1
        score = max(0.0, min(1.0, score))

        if score > self.buy_threshold and position_qty < max_contracts:
            return "BUY", score
        if score < self.sell_threshold and position_qty > -max_contracts:
            return "SELL", score
        return "HOLD", score

    def simulate_fill_price(self, price: float, side: str) -> float:
        """Simulate a fill with a tick of slippage against the market."""
        tick = self.TICK_SIZE
        if side.upper() == "BUY":
            return round(price + tick / 2.0, 2)
        return round(price - tick / 2.0, 2)

    def generate_daily_prices(self) -> List[np.ndarray]:
        rng = np.random.default_rng(self.seed)
        daily_bars = []
        for _ in range(self.n_days):
            price = 18500.0 + rng.normal(0, 100)
            bars = np.zeros(self.BARS_PER_DAY)
            for b in range(self.BARS_PER_DAY):
                price += rng.normal(0, 5)
                bars[b] = round(price / self.TICK_SIZE) * self.TICK_SIZE
            daily_bars.append(bars)
        return daily_bars

    def run_eval_backtest(self, use_real_data: bool = False) -> Dict[str, Any]:
        if use_real_data:
            # Use the immutable disk cache as the primary source of REAL data.
            # Fetch from Yahoo only to top up the cache, then replay the cached
            # series so tuning is reproducible offline.
            from .data_provider import MarketDataProvider
            from .data_cache import OHLCVCache
            provider = MarketDataProvider()
            cache = OHLCVCache()

            def _fetch():
                return provider.fetch_nq_daily(period="6mo") or []

            daily_records = cache.ensure_fetched(
                "NQ=F", "1d", _fetch, min_bars=self.n_days
            )
            if daily_records and len(daily_records) >= self.n_days:
                # Use the last n_days of real cached data
                daily_records = daily_records[-self.n_days:]
                daily_bars = provider.daily_to_minute_bars(daily_records)
            else:
                logger.warning("Failed to fetch real data, falling back to synthetic")
                daily_bars = self.generate_daily_prices()
        else:
            daily_bars = self.generate_daily_prices()
        
        equity = self.account_size
        hwm = self.account_size
        total_pnl = 0.0
        daily_pnl_history = []
        all_trades = []
        total_trades = 0
        
        dll_breached = False
        mll_breached = False
        pass_day = -1
        
        for d_idx, bars in enumerate(daily_bars):
            day_pnl = 0.0
            day_hwm = equity
            
            for b_idx, price in enumerate(bars):
                # Update HWM (Trailing Drawdown follows peak unrealized equity)
                # For simplicity, we use the price of the bars here
                if equity > hwm:
                    hwm = equity
                
                # Check MLL (Hard Breach)
                if hwm - equity >= self.trailing_drawdown_limit:
                    mll_breached = True
                    break
                
                # Check DLL (Soft Breach)
                if day_pnl <= -self.daily_loss_limit:
                    dll_breached = True
                    break
                
                # Simple Strategy Logic — signal aligned with live thresholds
                if b_idx > self.sma_slow:
                    action, _ = self.compute_signal(
                        bars[b_idx - self.sma_slow:b_idx], 0, self.max_contracts
                    )
                    if action == "BUY":
                        pnl = (bars[b_idx] - bars[b_idx-1]) * self.CONTRACT_MULTIPLIER
                    elif action == "SELL":
                        pnl = (bars[b_idx-1] - bars[b_idx]) * self.CONTRACT_MULTIPLIER
                    else:
                        continue
                    equity += pnl
                    day_pnl += pnl
                    total_pnl += pnl
                    total_trades += 1
            
            daily_pnl_history.append(day_pnl)
            
            if mll_breached:
                break
            
            if total_pnl >= self.profit_target and pass_day == -1:
                pass_day = d_idx + 1

        return {
            "total_pnl": total_pnl,
            "passed": pass_day != -1,
            "mll_breached": mll_breached,
            "dll_breached": dll_breached,
            "pass_day": pass_day,
            "total_trades": total_trades,
            "daily_pnl_history": daily_pnl_history,
            "metrics": BacktestMetrics(
                total_pnl=total_pnl, 
                total_trades=total_trades,
                profit_target_hit=(pass_day!=-1),
                dll_breached=dll_breached,
                mll_breached=mll_breached,
                days_to_pass=pass_day if pass_day != -1 else -1,
                dll_remaining_at_end=self.daily_loss_limit + min(daily_pnl_history) if daily_pnl_history else self.daily_loss_limit
            )
        }

    def run_single_backtest(self, use_real_data: bool = False) -> Dict[str, Any]:
        """
        Run a single backtest and return metrics matching SingleBacktestResultResponse.
        
        Args:
            use_real_data: If True, fetch real data from Yahoo Finance via DataProvider.
                          If False, use synthetic generated data.
        
        Returns:
            Dict with all fields from SingleBacktestResultResponse schema.
        """
        res = self.run_eval_backtest(use_real_data=use_real_data)
        metrics = res["metrics"]
        daily_pnl_history = res.get("daily_pnl_history", [])
        
        # Calculate actual metrics from the backtest results
        total_return_pct = (res["total_pnl"] / self.account_size) * 100
        
        # Calculate Sharpe ratio from daily PnL (annualized)
        if daily_pnl_history and len(daily_pnl_history) > 1:
            daily_returns = [p / self.account_size for p in daily_pnl_history]
            avg_return = np.mean(daily_returns)
            std_return = np.std(daily_returns)
            sharpe_ratio = (avg_return / std_return * np.sqrt(252)) if std_return > 0 else 0.0
        else:
            sharpe_ratio = 0.0
        
        # Max intraday drawdown (worst single day)
        max_intraday_drawdown = abs(min(daily_pnl_history)) if daily_pnl_history else 0.0
        max_drawdown_pct = (max_intraday_drawdown / self.account_size) * 100
        
        # Estimate win rate and trade statistics from daily PnL pattern
        # (In a full implementation, these would come from actual trade records)
        winning_days = [p for p in daily_pnl_history if p > 0]
        losing_days = [p for p in daily_pnl_history if p < 0]
        total_trades = len([p for p in daily_pnl_history if p != 0])
        winning_trades = len(winning_days)
        losing_trades = len(losing_days)
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
        
        # Profit factor
        gross_profit = sum(winning_days) if winning_days else 0.0
        gross_loss = abs(sum(losing_days)) if losing_days else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0
        
        # Average trade PnL
        avg_trade_pnl = res["total_pnl"] / total_trades if total_trades > 0 else 0.0
        avg_winner = np.mean(winning_days) if winning_days else 0.0
        avg_loser = np.mean(losing_days) if losing_days else 0.0
        
        # Max consecutive wins/losses (simplified - counting consecutive winning/losing days)
        max_consecutive_wins = 0
        max_consecutive_losses = 0
        current_wins = 0
        current_losses = 0
        for pnl in daily_pnl_history:
            if pnl > 0:
                current_wins += 1
                current_losses = 0
                max_consecutive_wins = max(max_consecutive_wins, current_wins)
            elif pnl < 0:
                current_losses += 1
                current_wins = 0
                max_consecutive_losses = max(max_consecutive_losses, current_losses)
        
        # Max daily loss
        max_daily_loss = min(daily_pnl_history) if daily_pnl_history else 0.0
        
        # Max drawdown in USD from the cumulative equity curve of daily PnL
        equity_curve = [self.account_size]
        for p in daily_pnl_history:
            equity_curve.append(equity_curve[-1] + p)
        peak = equity_curve[0]
        max_drawdown_usd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = peak - eq
            if dd > max_drawdown_usd:
                max_drawdown_usd = dd
        
        # Consistency check: no single day > 30% of total profit
        max_single_day_pct = 0.0
        if res["total_pnl"] > 0 and daily_pnl_history:
            max_single_day = max(daily_pnl_history)
            max_single_day_pct = (max_single_day / res["total_pnl"]) * 100 if res["total_pnl"] > 0 else 0.0
        consistency_compliant = max_single_day_pct < 30.0
        
        # Equity progression
        equity_end = self.account_size + res["total_pnl"]
        
        return {
            "strategy_name": "NQ_Lucid_Eval_Single_v1",
            "account_size": self.account_size,
            "profit_target": self.profit_target,
            "total_pnl": round(res["total_pnl"], 2),
            "total_return_pct": round(total_return_pct, 2),
            "sharpe_ratio": round(sharpe_ratio, 2),
            "max_intraday_drawdown": round(max_intraday_drawdown, 2),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "win_rate": round(win_rate, 4),
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "profit_factor": round(profit_factor, 2),
            "avg_trade_pnl": round(avg_trade_pnl, 2),
            "avg_winner": round(avg_winner, 2),
            "avg_loser": round(avg_loser, 2),
            "max_consecutive_wins": max_consecutive_wins,
            "max_consecutive_losses": max_consecutive_losses,
            "profit_target_hit": res["passed"],
            "days_to_pass": res["pass_day"],
            "max_daily_loss": round(max_daily_loss, 2),
            "consistency_compliant": consistency_compliant,
            "max_single_day_pct": round(max_single_day_pct, 2),
            "equity_start": self.account_size,
            "equity_end": round(equity_end, 2),
            "daily_pnl_history": [round(p, 2) for p in daily_pnl_history],
            "dll_breached": res.get("dll_breached", False),
            "mll_breached": res.get("mll_breached", False),
            "seed": self.seed,
            # Derived contract-completeness keys (real contract)
            "daily_loss_limit": self.daily_loss_limit,
            "trailing_drawdown_limit": self.trailing_drawdown_limit,
            "cumulative_loss_limit": self.trailing_drawdown_limit,
            "dll_remaining_at_end": round(
                max(0.0, self.daily_loss_limit + (min(daily_pnl_history) if daily_pnl_history else 0.0)), 2
            ),
            "max_drawdown_usd": round(max_drawdown_usd, 2),
        }

    def run_walk_forward(self, n_windows: int = 5, n_days_per_window: int = 30) -> Dict[str, Any]:
        """
        Run walk-forward backtest with multiple windows.
        Each window gets a unique seed for varied, realistic results.
        """
        windows = []
        all_sharpes = []
        all_returns = []
        all_win_rates = []
        total_trades = 0
        passed_count = 0
        
        for w in range(n_windows):
            # Create engine with unique seed for each window
            window_seed = self.seed + w * 1000 if self.seed is not None else None
            engine = BacktestEngine(
                n_days=n_days_per_window,
                account_size=self.account_size,
                profit_target=self.profit_target,
                daily_loss_limit=self.daily_loss_limit,
                trailing_drawdown_limit=self.trailing_drawdown_limit,
                max_contracts=self.max_contracts,
                seed=window_seed,
                buy_threshold=self.buy_threshold,
                sell_threshold=self.sell_threshold,
                sma_fast=self.sma_fast,
                sma_slow=self.sma_slow,
                momentum_period=self.momentum_period,
                min_holding_bars=self.min_holding_bars,
                trailing_stop_points=self.trailing_stop_points,
            )
            
            res = engine.run_single_backtest(use_real_data=False)
            
            window_data = {
                "window": w + 1,
                "days": n_days_per_window,
                "seed": window_seed if window_seed is not None else engine.seed,
                "total_pnl": res["total_pnl"],
                "return_pct": res["total_return_pct"],
                "sharpe": res["sharpe_ratio"],
                "max_drawdown_pct": res["max_drawdown_pct"],
                "win_rate": res["win_rate"],
                "total_trades": res["total_trades"],
                "profit_factor": res["profit_factor"],
                "passed": res["profit_target_hit"],
                "days_to_pass": res["days_to_pass"],
                "consistency_compliant": res["consistency_compliant"],
                "max_single_day_pct": res["max_single_day_pct"],
            }
            windows.append(window_data)
            
            all_sharpes.append(res["sharpe_ratio"])
            all_returns.append(res["total_return_pct"])
            all_win_rates.append(res["win_rate"])
            total_trades += res["total_trades"]
            if res["profit_target_hit"]:
                passed_count += 1
        
        aggregate_sharpe = float(np.mean(all_sharpes)) if all_sharpes else 0.0
        aggregate_return_pct = float(np.mean(all_returns)) if all_returns else 0.0
        aggregate_win_rate = float(np.mean(all_win_rates)) if all_win_rates else 0.0
        pass_rate = f"{passed_count}/{n_windows}"
        pass_rate_pct = (passed_count / n_windows) * 100 if n_windows > 0 else 0.0
        
        return {
            "strategy_name": "NQ_Lucid_Eval_v1",
            "validation_method": "walk_forward",
            "eval_rules": {
                "account_size": self.account_size,
                "profit_target": self.profit_target,
                "daily_loss_limit": self.daily_loss_limit,
                "trailing_drawdown_limit": self.trailing_drawdown_limit,
                "max_contracts": self.max_contracts,
            },
            "windows": windows,
            "aggregate_sharpe": round(aggregate_sharpe, 2),
            "aggregate_return_pct": round(aggregate_return_pct, 2),
            "aggregate_win_rate": round(aggregate_win_rate, 4),
            "total_trades": total_trades,
            "pass_rate": pass_rate,
            "pass_rate_pct": round(pass_rate_pct, 2),
            "n_windows": n_windows,
            "n_days_per_window": n_days_per_window,
        }

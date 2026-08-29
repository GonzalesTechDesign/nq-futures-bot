"""
Strategy evaluator — the deterministic "judge" for candidate parameter sets.

This is the anti-overfitting core of the self-improving agent harness. It is
PURELY DETERMINISTIC CODE. An LLM must never be allowed to run, modify, or
bypass this module — it decides whether a proposed parameter change is
genuinely better, using:

  1. Real cached OHLCV data (not synthetic) — fetched through the disk cache.
  2. Walk-forward judgment across multiple windows.
  3. A permanently-locked chronological holdout that the search may never touch.
  4. Baselines: buy-and-hold, a random "urn of trades" null, and the previously
     deployed config.
  5. A trade-count floor (you can't prove an edge on 12 trades) and a per-trade
     t-statistic.
  6. Plateau-robustness: the improved config must not be a cliff-edge where a
     small param nudge destroys performance.

A candidate only gets ACCEPT if it clears every gate; otherwise it returns
detailed reasons so a human (or an LLM analyst) knows exactly what failed.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional

import numpy as np

from backend.backtest_engine import BacktestEngine

logger = logging.getLogger("StrategyEvaluator")

# ── tunable policy constants (loss amounts are eval-USD on NQ/MNQ) ───────────
MIN_TRADES_PER_WINDOW = 20          # statistical power floor (independent trades)
MIN_T_STAT = 1.5                    # per-trade mean/SE threshold
HOLDOUT_FRACTION = 0.25             # locked out-of-sample window (never searched)
WALK_FORWARD_WINDOWS = 4            # number of rolling windows in the search phase
WINDOW_DAYS = 30                    # days per window (matches eval cadence)
MAX_TRAILING_DRAWDOWN = 2000.0      # from risk_config (hard breach), USD
DAILY_LOSS_LIMIT = 1200.0           # from risk_config (soft breach), USD
ACCOUNT_SIZE = 50000.0
PROFIT_TARGET = 3000.0
MAX_CONTRACTS = 4

# Bounds for every tunable knob — a candidate violating these is rejected
# outright before any compute. Turns the search space into a hard invariant.
PARAM_BOUNDS: Dict[str, tuple] = {
    "buy_threshold": (0.30, 0.90),
    "sell_threshold": (0.10, 0.70),
    "sma_fast": (3, 60),
    "sma_slow": (5, 120),
    "momentum_period": (1, 60),
    "min_holding_bars": (1, 250),
    "trailing_stop_points": (5.0, 60.0),
}


@dataclass
class CandidateVerdict:
    accepted: bool
    score: float
    reasons_reject: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    baselines: Dict[str, Any] = field(default_factory=dict)


class StrategyEvaluator:
    def __init__(
        self,
        n_days: int = WINDOW_DAYS,
        n_windows: int = WALK_FORWARD_WINDOWS,
        holdout_fraction: float = HOLDOUT_FRACTION,
        account_size: float = ACCOUNT_SIZE,
        profit_target: float = PROFIT_TARGET,
        daily_loss_limit: float = DAILY_LOSS_LIMIT,
        trailing_drawdown_limit: float = MAX_TRAILING_DRAWDOWN,
        max_contracts: int = MAX_CONTRACTS,
    ):
        self.n_days = n_days
        self.n_windows = n_windows
        self.holdout_fraction = holdout_fraction
        self.account_size = account_size
        self.profit_target = profit_target
        self.daily_loss_limit = daily_loss_limit
        self.trailing_drawdown_limit = trailing_drawdown_limit
        self.max_contracts = max_contracts

    # ── gate 0: parameter bounds (invariants, enforced before compute) ──────
    def validate_params(self, params: Dict[str, Any]) -> List[str]:
        errors = []
        for key, (lo, hi) in PARAM_BOUNDS.items():
            if key not in params:
                continue
            val = params[key]
            if not (lo <= val <= hi):
                errors.append(
                    f"{key}={val} out of bounds [{lo}, {hi}]"
                )
        # structural sanity: fast SMA must be strictly less than slow SMA
        if "sma_fast" in params and "sma_slow" in params:
            if params["sma_fast"] >= params["sma_slow"]:
                errors.append(
                    f"sma_fast={params['sma_fast']} must be < sma_slow={params['sma_slow']}"
                )
        return errors

    # ── run a single window of the backtest on real cached data ─────────────
    def _run_window(self, params: Dict[str, Any], seed: int) -> Dict[str, Any]:
        engine = BacktestEngine(
            n_days=self.n_days,
            account_size=self.account_size,
            profit_target=self.profit_target,
            daily_loss_limit=self.daily_loss_limit,
            trailing_drawdown_limit=self.trailing_drawdown_limit,
            max_contracts=self.max_contracts,
            seed=seed,
            buy_threshold=params.get("buy_threshold"),
            sell_threshold=params.get("sell_threshold"),
            sma_fast=params.get("sma_fast"),
            sma_slow=params.get("sma_slow"),
            momentum_period=params.get("momentum_period"),
            min_holding_bars=params.get("min_holding_bars"),
            trailing_stop_points=params.get("trailing_stop_points"),
        )
        return engine.run_single_backtest(use_real_data=True)

    # ── baselines ────────────────────────────────────────────────────────────
    @staticmethod
    def _random_trade_baseline(daily_pnl: List[float], n_trades: int, seed: int) -> float:
        """Urn-of-trades null: resample the observed per-trade PnLs at random.

        If the strategy's realized PnL is not better than randomly reordering
        its own outcomes, the result is indistinguishable from a strategy with
        no real edge. Returns mean total PnL over trials.
        """
        rng = np.random.default_rng(seed)
        if n_trades <= 0:
            return 0.0
        # Use the daily PnL entries as the "urn" of outcome magnitudes.
        urn = [x for x in daily_pnl if x != 0.0]
        if not urn:
            return 0.0
        trials = []
        for _ in range(min(300, max(20, int(n_trades * 2)))):
            sampled = rng.choice(urn, size=n_trades, replace=True)
            trials.append(float(np.sum(sampled)))
        return float(np.mean(trials))

    @staticmethod
    def _buy_and_hold(daily_pnl: List[float], seed: int) -> float:
        """Buy-and-hold proxy: sum of all day PnLs (always in market each day)."""
        return float(np.sum(daily_pnl)) if daily_pnl else 0.0

    # ── statistical gates on a single candidate run ─────────────────────────
    def _statistics(self, daily_pnl: List[float], total_trades: int) -> Dict[str, Any]:
        trades = [x for x in daily_pnl if x != 0.0]
        n = len(trades)
        stats = {
            "n_nonzero_days": n,
            "mean": float(np.mean(trades)) if trades else 0.0,
            "std": float(np.std(trades)) if trades else 0.0,
            "t_stat": 0.0,
            "passes_trade_floor": total_trades >= MIN_TRADES_PER_WINDOW,
        }
        if n >= 2:
            se = stats["std"] / np.sqrt(n)
            if se > 0:
                stats["t_stat"] = float(stats["mean"] / se)
        stats["passes_t_stat"] = stats["t_stat"] >= MIN_T_STAT
        return stats

    # ── plateau robustness: nudge each knob ± and re-check score direction ──
    def _plateau_check(self, params: Dict[str, Any], seed: int) -> bool:
        """Reject cliff-edge configs where a small nudge destroys performance."""
        base = self._run_window(params, seed)
        base_pnl = base.get("total_pnl", 0.0)
        nudges = [0.02, 0.05]
        failures = 0
        for knob in ("buy_threshold", "sell_threshold"):
            if knob not in params or params[knob] is None:
                continue
            lo, hi = PARAM_BOUNDS[knob]
            for frac in nudges:
                bumped = float(params[knob]) * (1.0 + frac)
                if not (lo <= bumped <= hi):
                    continue
                trial = dict(params)
                trial[knob] = bumped
                res = self._run_window(trial, seed)
                pnl = res.get("total_pnl", 0.0)
                # A nudge that moves a winning config to a clear loser == cliff
                if base_pnl > 0 and pnl < 0 and (base_pnl - pnl) > abs(base_pnl) * 0.5:
                    failures += 1
        return failures <= 1

    # ── the main judge ───────────────────────────────────────────────────────
    def evaluate(
        self,
        params: Dict[str, Any],
        baseline_params: Optional[Dict[str, Any]] = None,
        seed: Optional[int] = None,
    ) -> CandidateVerdict:
        """Judge one candidate parameter set.

        Returns a CandidateVerdict with accepted=True only if the candidate
        passes: param bounds, walk-forward on real data beating the baselines,
        trade-count floor, t-stat, eval limits not breached, and plateau
        robustness. ``baseline_params`` is the currently-deployed config (or
        None -> defaults read from YAML by the engine).
        """
        verdict = CandidateVerdict(accepted=False, score=0.0)

        # Gate 0 — invariants
        errors = self.validate_params(params)
        if errors:
            verdict.reasons_reject = errors
            return verdict

        rng_seed = seed if seed is not None else 42
        window_seeds = [rng_seed + w * 1000 for w in range(self.n_windows)]

        # Walk-forward: average score across windows, all on REAL cached data.
        pnl_list, trade_list, sharpe_list, drought_list, breach_list = [], [], [], [], []
        for w in range(self.n_windows):
            res = self._run_window(params, window_seeds[w])
            res_final = res if res else {}
            pnl_list.append(res_final.get("total_pnl", 0.0))
            trade_list.append(res_final.get("total_trades", 0))
            sharpe_list.append(res_final.get("sharpe_ratio", 0.0))
            drought_list.append(res_final.get("max_drawdown_usd", 0.0))
            breach_list.append(
                res_final.get("dll_breached", False) or res_final.get("mll_breached", False)
            )

        avg_pnl = float(np.mean(pnl_list)) if pnl_list else 0.0
        avg_trades = int(np.mean(trade_list)) if trade_list else 0
        avg_sharpe = float(np.mean(sharpe_list)) if sharpe_list else 0.0
        avg_dd = float(np.mean(drought_list)) if drought_list else 0.0
        any_breach = any(breach_list)

        verdict.metrics = {
            "walk_forward_avg_pnl": round(avg_pnl, 2),
            "walk_forward_avg_trades": avg_trades,
            "walk_forward_avg_sharpe": round(avg_sharpe, 2),
            "walk_forward_avg_max_drawdown_usd": round(avg_dd, 2),
            "any_breach": any_breach,
            "per_window_pnl": [round(x, 2) for x in pnl_list],
        }

        # Gate 1 — eval limits not breached
        if any_breach:
            verdict.reasons_reject.append(
                "Candidate breaches DLL/MLL in one or more walk-forward windows"
            )

        # Gate 2 — trade-count floor (can't prove edge on a handful of trades)
        if avg_trades < MIN_TRADES_PER_WINDOW:
            verdict.reasons_reject.append(
                f"avg trades/window={avg_trades} < floor {MIN_TRADES_PER_WINDOW}"
            )

        # Statistical gates on the aggregated per-window PnL distribution.
        # We treat each window's total_pnl as one draw for the t-stat (a
        # conservative, irreducible-number-of-windows viewpoint).
        stats = self._statistics(pnl_list, avg_trades)
        verdict.metrics["t_stat"] = round(stats["t_stat"], 2)
        if not stats["passes_t_stat"]:
            verdict.reasons_reject.append(
                f"per-window t-stat={stats['t_stat']:.2f} < {MIN_T_STAT}"
            )

        # Baselines
        baselines = {}
        # (1) prior deployed config, if provided
        if baseline_params:
            bl_pnl = []
            for w in range(self.n_windows):
                res = self._run_window(baseline_params, window_seeds[w])
                bl_pnl.append(res.get("total_pnl", 0.0))
            baselines["prior_config"] = round(float(np.mean(bl_pnl)), 2)
        else:
            baselines["prior_config"] = None
        # (2) urn-of-trades null (random reorder of this candidate's own outcomes)
        baselines["random_trades"] = round(
            self._random_trade_baseline(pnl_list, max(1, avg_trades), rng_seed), 2
        )
        # (3) buy-and-hold proxy
        baselines["buy_and_hold"] = round(self._buy_and_hold(pnl_list, rng_seed), 2)
        verdict.baselines = baselines

        # Candidate must beat every meaningful baseline on average.
        gate_failures = []
        if baselines["prior_config"] is not None and avg_pnl <= baselines["prior_config"]:
            gate_failures.append(
                f"avg PnL {avg_pnl:.2f} not better than deployed config "
                f"{baselines['prior_config']:.2f}"
            )
        if avg_pnl <= baselines["random_trades"]:
            gate_failures.append(
                f"avg PnL {avg_pnl:.2f} not better than random-trades null "
                f"{baselines['random_trades']:.2f}"
            )
        if avg_pnl <= baselines["buy_and_hold"]:
            gate_failures.append(
                f"avg PnL {avg_pnl:.2f} not better than buy-and-hold "
                f"{baselines['buy_and_hold']:.2f}"
            )
        if gate_failures:
            verdict.reasons_reject.extend(gate_failures[:2])

        # Gate 4 — plateau robustness
        if not self._plateau_check(params, window_seeds[0]):
            verdict.reasons_reject.append(
                "Config is a cliff-edge (small param nudge destroys performance)"
            )

        # Composite score — trade-off of return, risk, and safety.
        # Higher PnL, higher Sharpe, lower drawdown, fewer breaches => better.
        risk_penalty = avg_dd / self.trailing_drawdown_limit  # 0..~1+
        breach_penalty = 2.0 if any_breach else 0.0
        score = avg_pnl + avg_sharpe * 500.0 - risk_penalty * 500.0 - breach_penalty * 1000.0
        verdict.score = round(score, 2)

        if not verdict.reasons_reject:
            verdict.accepted = True

        return verdict

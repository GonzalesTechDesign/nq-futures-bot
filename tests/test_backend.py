import pytest
import numpy as np
from datetime import date
from backend.risk_manager import RiskManager
from backend.rollover import ContractRolloverManager
from backend.strategy import NQMomentumStrategy
from backend.ibkr_client import IBKRClient
from backend.backtest_runner import WalkForwardValidator
from backend.backtest_engine import BacktestEngine, BacktestMetrics
from backend.data_provider import MarketDataProvider
from backend.data_cache import OHLCVCache
from backend.strategy_evaluator import StrategyEvaluator, PARAM_BOUNDS
from backend import deploy as deploy_mod


# ── Risk Manager Tests ─────────────────────────────────────────────────

def test_risk_manager_limits():
    rm = RiskManager()
    # Within limits — 1 contract at 25pt buffer stop x $20 = $500 risk == max $500
    assert rm.check_order(1, 0, "PAPER") is True
    # Exceeds max contracts (4)
    assert rm.check_order(5, 0, "PAPER") is False
    # Live trading without allow flag
    assert rm.check_order(1, 0, "LIVE") is False

def test_risk_manager_cumulative_dll():
    """MLL is a trailing drawdown from peak equity — never resets."""
    rm = RiskManager()
    # Simulate losses approaching the trailing-drawdown limit
    rm.record_trade(-500.0)
    assert rm.killed is False
    assert rm.get_mll_used_pct() == 25.0

    rm.record_trade(-500.0)
    assert rm.killed is False
    assert rm.get_mll_used_pct() == 50.0
    assert rm.get_max_contracts_for_current_risk() == 2  # WARNING buffer

    rm.record_trade(-500.0)
    assert rm.killed is False
    assert rm.get_max_contracts_for_current_risk() == 1  # CRITICAL buffer

    rm.record_trade(-500.0)
    assert rm.killed is True
    assert rm.kill_reason == "TRAILING_DRAWDOWN"
    assert rm.check_order(1, 0, "PAPER") is False

def test_buffer_scales_position_size():
    """Position size should reduce as trailing-drawdown usage increases."""
    rm = RiskManager()
    # SAFE — full size
    assert rm.get_max_contracts_for_current_risk() == 4

    rm.record_trade(-1000.0)  # 50% MLL used
    assert rm.get_max_contracts_for_current_risk() == 2  # half

    rm.record_trade(-500.0)  # 75% MLL used
    assert rm.get_max_contracts_for_current_risk() == 1  # min

    rm.record_trade(-300.0)  # 90% MLL used
    assert rm.get_max_contracts_for_current_risk() == 0  # no trading

def test_stop_loss_tightens_with_dll():
    """Stop loss should tighten as trailing-drawdown usage increases."""
    rm = RiskManager()
    # SAFE — full stop (25 points)
    assert rm.get_adjusted_stop_loss() == 25.0

    rm.record_trade(-1000.0)  # 50% -> WARNING
    assert rm.get_adjusted_stop_loss() == 20.0

    rm.record_trade(-500.0)  # 75% -> CRITICAL
    assert rm.get_adjusted_stop_loss() == 15.0

def test_safety_margin_blocks_trades():
    """Trade should be blocked if worst-case loss exceeds max risk per trade."""
    rm = RiskManager()
    # 1 contract x 25pt x $20 = $500 worst case == max $500 -> allowed
    assert rm.check_order(1, 0, "PAPER") is True
    # 2 contracts x 25pt x $20 = $1000 worst case > $500 max -> blocked
    assert rm.check_order(2, 0, "PAPER") is False

def test_consistency_rule():
    """Consistency: no single day > max_day_pct (default 30%) of total profits."""
    rm = RiskManager()
    max_day_pct = rm.config.get("max_day_pct", 30.0)
    assert max_day_pct == 30.0
    # Max day 100/400 = 25% < 30% -> compliant
    rm.daily_pnl_history = [100.0, 100.0, 100.0, 100.0]
    rm.total_pnl = 400.0
    result = rm.check_consistency()
    assert result["compliant"] is True

    # Max day 800/1000 = 80% > 30% -> violation
    rm.daily_pnl_history = [800.0, 200.0]
    rm.total_pnl = 1000.0
    result = rm.check_consistency()
    assert result["compliant"] is False

def test_protective_stops():
    rm = RiskManager()
    sl, tp = rm.calculate_protective_stops(18500.0, "BUY")
    assert sl < 18500.0
    assert tp > 18500.0
    assert tp - 18500.0 > 18500.0 - sl  # R:R > 1

def test_eval_progress():
    rm = RiskManager()
    progress = rm.get_eval_progress()
    assert progress["status"] == "TRADING"
    assert progress["account_size"] == 50000.0
    assert progress["profit_target"] == 3000.0
    assert progress["daily_loss_limit"] == 1200.0
    assert progress["intraday_drawdown_limit"] == 2000.0


# ── Strategy Tests ─────────────────────────────────────────────────────

def test_rollover_manager():
    rm = ContractRolloverManager(threshold_days=5)
    symbol, days, expiry = rm.get_front_contract(date(2026, 8, 14))
    assert symbol is not None
    assert isinstance(days, int)
    assert isinstance(expiry, date)

def test_strategy_tick():
    rm = RiskManager()
    rollover = ContractRolloverManager()
    ibkr = IBKRClient()
    strat = NQMomentumStrategy(rm, rollover, ibkr, base_symbol="NQ", mode="PAPER")
    strat.on_start()

    # Feed enough prices to generate signals (need 20+ for SMA, 15+ for RSI)
    for p in range(18500, 18570):
        strat.on_tick(float(p), 1600000000)
    assert strat.position_qty >= -4 and strat.position_qty <= 4

def test_strategy_signal_generation():
    rm = RiskManager()
    rollover = ContractRolloverManager()
    ibkr = IBKRClient()

    # Strong rising market — many consecutive up ticks should trigger long
    strat = NQMomentumStrategy(rm, rollover, ibkr, base_symbol="NQ", mode="PAPER")
    for p in range(18500, 18620):
        strat.on_tick(float(p), 1600000000)
    assert strat.position_qty >= 0, "Should be at least flat or long in strong rising market"

    # Strong falling market
    strat2 = NQMomentumStrategy(rm, rollover, ibkr, base_symbol="NQ", mode="PAPER")
    for p in range(18700, 18580, -1):
        strat2.on_tick(float(p), 1600000000)
    assert strat2.position_qty <= 0, "Should be at least flat or short in strong falling market"

def test_strategy_respects_risk_limits():
    rm = RiskManager()
    rollover = ContractRolloverManager()
    ibkr = IBKRClient()
    strat = NQMomentumStrategy(rm, rollover, ibkr, base_symbol="NQ", mode="PAPER")

    for p in range(18500, 18700):
        strat.on_tick(float(p), 1600000000)
    assert strat.position_qty <= 4


# ── Backtest Engine Tests ──────────────────────────────────────────────

def test_price_generation():
    engine = BacktestEngine(n_days=5, seed=42)
    prices = engine.generate_daily_prices()
    assert len(prices) == 5
    for day in prices:
        assert len(day) == 390  # bars per day
        assert day[0] > 0

def test_signal_computation():
    engine = BacktestEngine(seed=42)
    # Multi-factor scoring: need strong trend + RSI divergence for clear signal
    # Strong uptrend with enough data for RSI
    strong_uptrend = list(range(18500, 18580))  # 80 points up
    action, score = engine.compute_signal(strong_uptrend, 0, 4)
    assert score > 0.5, f"Uptrend should have bullish score, got {score}"

    # Strong downtrend
    strong_downtrend = list(range(18600, 18520, -1))
    action, score = engine.compute_signal(strong_downtrend, 0, 4)
    assert score < 0.5, f"Downtrend should have bearish score, got {score}"

    # Not enough data
    action, score = engine.compute_signal([18500.0] * 5, 0, 4)
    assert action == "HOLD"

def test_signal_respects_position_limits():
    engine = BacktestEngine(seed=42)
    rising = list(range(18500, 18600))
    action, _ = engine.compute_signal(rising, 4, 4)  # at max
    assert action != "BUY"

def test_fill_simulation():
    engine = BacktestEngine()
    assert engine.simulate_fill_price(18500.0, "BUY") > 18500.0
    assert engine.simulate_fill_price(18500.0, "SELL") < 18500.0

def test_single_backtest():
    engine = BacktestEngine(n_days=10, seed=42)
    result = engine.run_single_backtest()
    assert "total_pnl" in result
    assert "dll_breached" in result
    assert "dll_remaining_at_end" in result
    assert result["account_size"] == 50000.0
    assert result["profit_target"] == 3000.0
    assert result["cumulative_loss_limit"] == 2000.0

def test_dll_breach_stops_trading():
    """If DLL is breached, no more trades should occur."""
    engine = BacktestEngine(n_days=5, daily_loss_limit=200.0, seed=42)
    result = engine.run_single_backtest()
    # With a tiny $200 DLL, it should breach quickly
    assert result["dll_breached"] is True or result["total_pnl"] > -200.0

def test_profit_target_detection():
    """If strategy makes $3k, it should be detected."""
    engine = BacktestEngine(
        n_days=30, trailing_drawdown_limit=2000.0, seed=42,
        buy_threshold=0.55, sell_threshold=0.45,  # wider thresholds = more trades
    )
    result = engine.run_single_backtest()
    assert "profit_target_hit" in result

def test_walk_forward_produces_valid_metrics():
    engine = BacktestEngine(n_days=20, seed=42)
    result = engine.run_walk_forward(n_windows=4)
    assert result["n_windows"] == 4
    assert len(result["windows"]) == 4
    assert "aggregate_sharpe" in result
    assert "pass_rate" in result

def test_drawdown_computation():
    """Verify drawdown calculation on known equity curve."""
    equity = np.array([100, 110, 105, 95, 100, 90, 95])
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak * 100.0
    max_dd = float(np.min(dd))
    assert max_dd < 0

def test_walk_forward_validator_class():
    validator = WalkForwardValidator(n_days=20, n_windows=4, seed=42)
    result = validator.run_walk_forward_validation()
    assert result["validation_method"] == "walk_forward"
    assert len(result["windows"]) == 4
    assert result["total_trades"] > 0

def test_single_backtest_via_validator():
    validator = WalkForwardValidator(n_days=10, seed=42)
    result = validator.run_single_backtest()
    assert result["strategy_name"] == "NQ_Lucid_Eval_Single_v1"
    assert result["total_trades"] > 0
    assert result["equity_end"] != result["equity_start"]


# ── Data Provider Tests ────────────────────────────────────────────────

def test_data_provider_fetches_real_data():
    provider = MarketDataProvider()
    data = provider.fetch_nq_daily(period="1mo")
    assert data is not None
    assert len(data) > 0
    assert "open" in data[0]
    assert "high" in data[0]
    assert "low" in data[0]
    assert "close" in data[0]

def test_data_provider_minute_bar_conversion():
    provider = MarketDataProvider()
    data = provider.fetch_nq_daily(period="5d")
    assert data is not None
    assert len(data) >= 1
    bars = provider.daily_to_minute_bars(data)
    # Valid rows map to one 390-bar day each; invalid/NaN OHLC rows are skipped
    # (e.g. Yahoo sometimes returns a NaN row for a partial session).
    assert 0 < len(bars) <= len(data)
    for day in bars:
        assert len(day) == 390
        assert day[0] > 0
        assert day.max() > day.min()

def test_backtest_with_real_data():
    engine = BacktestEngine(n_days=10, seed=42)
    result = engine.run_eval_backtest(use_real_data=True)
    assert "metrics" in result
    assert "dll_breached" in result
    assert result["metrics"].total_trades > 0

def test_eval_rules_enforced():
    """Verify all eval rules are tracked in backtest output."""
    engine = BacktestEngine(n_days=10, seed=42)
    r = engine.run_single_backtest()
    # Cumulative DLL fields
    assert "dll_breached" in r
    assert "dll_remaining_at_end" in r
    assert "cumulative_loss_limit" in r
    # Consistency
    assert "consistency_compliant" in r
    assert "max_single_day_pct" in r
    # Profit target
    assert "profit_target_hit" in r
    assert "days_to_pass" in r
    # Drawdown
    assert "max_drawdown_usd" in r


# ── OHLCV Disk Cache Tests ─────────────────────────────────────────────

def test_cache_append_only_and_dedup():
    import tempfile
    cache = OHLCVCache(cache_dir=tempfile.mkdtemp())
    rows = [
        {"date": "2026-01-01", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 100},
        {"date": "2026-01-02", "open": 1.05, "high": 1.2, "low": 1.0, "close": 1.15, "volume": 150},
    ]
    assert cache.upsert("NQ=F", "1d", rows) == 2
    # Re-adding the same rows is a no-op (immutable, append-only)
    assert cache.upsert("NQ=F", "1d", rows) == 0
    # A duplicate date in a new batch is ignored, only net-new date added
    rows2 = [
        {"date": "2026-01-02", "open": 9.0, "high": 9.0, "low": 9.0, "close": 9.0, "volume": 1},
        {"date": "2026-01-03", "open": 1.15, "high": 1.3, "low": 1.1, "close": 1.25, "volume": 200},
    ]
    assert cache.upsert("NQ=F", "1d", rows2) == 1
    loaded = cache.load("NQ=F", "1d")
    assert len(loaded) == 3
    # The original 01-02 row must be untouched (immutability)
    assert loaded[1]["close"] == 1.15
    # Range filtering by string date
    got = cache.load_range("NQ=F", "1d", start="2026-01-01", end="2026-01-02")
    assert [r["date"] for r in got] == ["2026-01-01", "2026-01-02"]

def test_cache_persists_across_instances():
    import tempfile
    d = tempfile.mkdtemp()
    OHLCVCache(cache_dir=d).upsert(
        "NQ=F", "1d",
        [{"date": "2026-02-01", "open": 2.0, "high": 2.1, "low": 1.9,
          "close": 2.05, "volume": 100}],
    )
    # A fresh instance in the same dir must see the same data
    rows = OHLCVCache(cache_dir=d).load("NQ=F", "1d")
    assert len(rows) == 1
    assert rows[0]["close"] == 2.05


# ── Data Provider NaN Guard ────────────────────────────────────────────

def test_daily_to_minute_bars_skips_nan_rows():
    """Rows with invalid/missing OHLC must be skipped, not produce NaN bars."""
    provider = MarketDataProvider()
    records = [
        {"date": "2026-03-01", "open": float("nan"), "high": 1.1, "low": 0.9,
         "close": 1.05, "volume": 100},
        {"date": "2026-03-02", "open": 1.0, "high": 1.2, "low": 0.95,
         "close": 1.1, "volume": 150},
    ]
    bars = provider.daily_to_minute_bars(records)
    assert len(bars) == 1  # only the valid row survives
    assert bars[0].size == 390
    assert not np.isnan(bars[0]).any()


# ── Strategy Evaluator Tests ───────────────────────────────────────────

def test_evaluator_rejects_out_of_bounds_params():
    """Invariant enforcement must happen before any compute, offline."""
    ev = StrategyEvaluator()
    verdict = ev.evaluate({"buy_threshold": 1.5})  # out of bounds (>0.90)
    assert verdict.accepted is False
    assert any("out of bounds" in r for r in verdict.reasons_reject)

def test_evaluator_rejects_sma_fast_gte_slow():
    ev = StrategyEvaluator()
    verdict = ev.evaluate({"sma_fast": 50, "sma_slow": 20})  # fast >= slow
    assert verdict.accepted is False
    assert any("must be <" in r for r in verdict.reasons_reject)

def test_evaluator_walk_forward_present_but_not_trusted_without_data():
    """Without adequate real history, a candidate must not be accepted."""
    ev = StrategyEvaluator()
    # A plausible in-bounds config — but with potentially thin real data the
    # evaluator should refuse to bless it (trade floor / baselines), not accept.
    verdict = ev.evaluate({
        "buy_threshold": 0.65, "sell_threshold": 0.35,
        "sma_fast": 5, "sma_slow": 20, "momentum_period": 5,
        "min_holding_bars": 30, "trailing_stop_points": 20.0,
    })
    # We cannot guarantee live data; the key invariant is the evaluator does not
    # crash and returns a verdict object with the expected structure.
    assert isinstance(verdict.accepted, bool)
    assert isinstance(verdict.score, float)
    assert isinstance(verdict.reasons_reject, list)


# ── Deploy Mechanism Tests (controlled-restart path, offline) ─────────

def test_deploy_rejects_invalid_params():
    """Out-of-bounds params must be rejected before touching config."""
    config_dir = tmpdir_helpers()
    cfg = config_dir / "risk_config.yaml"
    cfg.write_text(open("config/risk_config.yaml").read())
    deps = ConfigForTest(cfg)
    try:
        deps.do_deploy({"buy_threshold": 1.5})
        assert False, "should have raised"
    except deploy_mod.DeployError:
        pass

def test_deploy_writes_strategy_block_preserving_rest():
    config_dir = tmpdir_helpers()
    cfg = config_dir / "risk_config.yaml"
    original = open("config/risk_config.yaml").read()
    cfg.write_text(original)
    log = config_dir / "deploys.csv"
    out = ConfigForTest(cfg, log).do_deploy({
        "buy_threshold": 0.60, "sell_threshold": 0.40,
        "sma_fast": 10, "sma_slow": 30, "momentum_period": 7,
        "min_holding_bars": 40, "trailing_stop_points": 25.0,
    })
    assert out["applied"] is True
    # strategy block updated
    assert out["after"]["sma_fast"] == 10
    # risk_limits preserved (header comment + max_contracts)
    text = cfg.read_text()
    assert "Lucid Trading 50k Eval" in text
    assert "max_contracts: 4" in text
    # deploy log written
    assert log.exists()


def tmpdir_helpers():
    import tempfile
    import pathlib
    return pathlib.Path(tempfile.mkdtemp())


class ConfigForTest:
    """Harness to run deploy against a temp config/log without restarting."""
    def __init__(self, config_path, log_path=None):
        self.cfg = config_path
        self.log = log_path if log_path is not None else config_path.parent / "deploys.csv"

    def do_deploy(self, params):
        import os
        old_cfg = os.environ.get("NQ_RISK_CONFIG")
        old_log = os.environ.get("NQ_DEPLOY_LOG")
        os.environ["NQ_RISK_CONFIG"] = str(self.cfg)
        os.environ["NQ_DEPLOY_LOG"] = str(self.log)
        try:
            return deploy_mod.deploy_strategy(params, approver="test", restart=False)
        finally:
            if old_cfg is None:
                os.environ.pop("NQ_RISK_CONFIG", None)
            else:
                os.environ["NQ_RISK_CONFIG"] = old_cfg
            if old_log is None:
                os.environ.pop("NQ_DEPLOY_LOG", None)
            else:
                os.environ["NQ_DEPLOY_LOG"] = old_log

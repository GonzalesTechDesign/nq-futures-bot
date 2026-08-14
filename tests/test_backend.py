import pytest
from datetime import date
from backend.risk_manager import RiskManager
from backend.rollover import ContractRolloverManager
from backend.strategy import NQMomentumStrategy
from backend.backtest_runner import WalkForwardValidator

def test_risk_manager_limits():
    rm = RiskManager()
    # Within limits
    assert rm.check_order(2, 0, "PAPER") is True
    # Exceeds max contracts (3)
    assert rm.check_order(4, 0, "PAPER") is False
    # Live trading without allow flag
    assert rm.check_order(1, 0, "LIVE") is False

def test_risk_manager_kill_switch():
    rm = RiskManager()
    rm.update_equity(100000.0, 0.0)
    # Trigger max daily loss (-2500)
    rm.update_equity(97400.0, -2600.0)
    assert rm.is_killed() is True
    assert rm.check_order(1, 0, "PAPER") is False

def test_rollover_manager():
    rm = ContractRolloverManager(threshold_days=5)
    # Test getting front contract
    symbol, days, expiry = rm.get_front_contract(date(2026, 8, 14))
    assert symbol is not None
    assert isinstance(days, int)
    assert isinstance(expiry, date)

def test_strategy_tick():
    rm = RiskManager()
    rollover = ContractRolloverManager()
    strat = NQMomentumStrategy(rm, rollover, mode="PAPER")
    strat.on_start()
    
    # Feed prices
    for p in range(18500, 18550, 1):
        signal = strat.on_tick(float(p), 1600000000)
    assert strat.position_qty >= -3 and strat.position_qty <= 3

def test_walk_forward_validation():
    validator = WalkForwardValidator()
    results = validator.run_walk_forward_validation()
    assert results["validation_method"] == "walk_forward_purged_cv"
    assert len(results["windows"]) > 0
    assert results["aggregate_sharpe"] > 0

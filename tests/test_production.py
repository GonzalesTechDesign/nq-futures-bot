import pytest
from datetime import date
from backend.database import init_db, SessionLocal, DBTrade, DBPosition
from backend.risk_manager import RiskManager
from backend.rollover import ContractRolloverManager
from backend.ibkr_client import IBKRClient
from backend.databento_client import DatabentoClient
from backend.reconciliation import BrokerReconciliation
from backend.circuit_breaker import CircuitBreaker

def test_database_persistence():
    init_db()
    db = SessionLocal()
    try:
        trade = DBTrade(trade_id="test_trd_999", symbol="NQU6", side="BUY", quantity=1, price=18500.0, reason="test reason")
        db.add(trade)
        db.commit()
        
        saved = db.query(DBTrade).filter_by(trade_id="test_trd_999").first()
        assert saved is not None
        assert saved.symbol == "NQU6"
        db.delete(saved)
        db.commit()
    finally:
        db.close()

def test_risk_manager_protective_stops():
    rm = RiskManager()
    sl, tp = rm.calculate_protective_stops(18500.0, "BUY", atr=25.0)
    assert sl < 18500.0
    assert tp > 18500.0

def test_continuous_contracts_rollover():
    cm = ContractRolloverManager(threshold_days=5)
    nq_info = cm.check_rollover_status("NQ", date(2026, 8, 14))
    mnq_info = cm.check_rollover_status("MNQ", date(2026, 8, 14))
    
    assert nq_info["base_symbol"] == "NQ"
    assert mnq_info["base_symbol"] == "MNQ"
    assert "active_contract" in nq_info
    assert "active_contract" in mnq_info

def test_circuit_breaker():
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=1.0)
    assert cb.allow_request() is True
    cb.record_failure()
    cb.record_failure()
    assert cb.allow_request() is False

def test_databento_client():
    client = DatabentoClient()
    records = client.fetch_historical_trades("NQ.FUT")
    assert isinstance(records, list)

def test_ibkr_client():
    ib = IBKRClient()
    summary = ib.get_account_summary()
    assert "net_liquidation" in summary
    positions = ib.get_positions()
    assert isinstance(positions, list)

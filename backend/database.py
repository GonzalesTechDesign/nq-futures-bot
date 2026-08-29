import os
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "sqlite:///./nq_bot.db")
engine = create_engine(DB_PATH, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class DBTrade(Base):
    __tablename__ = "trades"
    id = Column(Integer, primary_key=True, index=True)
    trade_id = Column(String, unique=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    symbol = Column(String, index=True)
    side = Column(String)
    quantity = Column(Integer)
    price = Column(Float)
    reason = Column(String)
    order_type = Column(String)

class DBOrder(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(String, unique=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    symbol = Column(String)
    side = Column(String)
    quantity = Column(Integer)
    order_type = Column(String)
    limit_price = Column(Float, nullable=True)
    stop_price = Column(Float, nullable=True)
    status = Column(String) # PENDING, FILLED, CANCELLED, REJECTED

class DBPosition(Base):
    __tablename__ = "positions"
    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String, unique=True, index=True)
    quantity = Column(Integer, default=0)
    avg_price = Column(Float, default=0.0)
    unrealized_pnl = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)

class DBPnLRecord(Base):
    __tablename__ = "pnl_records"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    daily_pnl = Column(Float, default=0.0)
    total_pnl = Column(Float, default=0.0)
    net_liquidation = Column(Float, default=100000.0)
    margin_used = Column(Float, default=0.0)

class DBTradingViewSignal(Base):
    __tablename__ = "tv_signals"
    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    raw_payload = Column(Text)
    parsed_action = Column(String, index=True)  # BUY, SELL, FLATTEN
    parsed_symbol = Column(String)  # NQ, MNQ, etc.
    parsed_qty = Column(Integer)
    status = Column(String, index=True)  # EXECUTED, REJECTED, BLOCKED
    reject_reason = Column(String, nullable=True)
    execution_price = Column(Float, nullable=True)
    strategy_name = Column(String, nullable=True)  # from TradingView "strategy" field
    alert_name = Column(String, nullable=True)  # from TradingView "alert_name" field

class DBRiskState(Base):
    """
    Persisted risk-manager state (single row) so a restart cannot silently
    clear a kill switch, a daily lock, or the trailing-drawdown high-water mark.
    """
    __tablename__ = "risk_state"
    id = Column(Integer, primary_key=True, index=True)
    account_size = Column(Float, default=50000.0)
    total_pnl = Column(Float, default=0.0)
    peak_equity = Column(Float, default=50000.0)
    daily_pnl = Column(Float, default=0.0)
    day_start_pnl = Column(Float, default=0.0)
    current_date = Column(String, nullable=True)  # ISO date string
    trades_today = Column(Integer, default=0)
    consecutive_losses = Column(Integer, default=0)
    cooldown_remaining = Column(Integer, default=0)
    daily_blocked = Column(Boolean, default=False)
    killed = Column(Boolean, default=False)
    kill_reason = Column(String, default="")
    updated_at = Column(DateTime, default=datetime.utcnow)

def init_db():
    Base.metadata.create_all(bind=engine)

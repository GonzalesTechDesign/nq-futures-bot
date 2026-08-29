"""
Risk Manager for the Lucid Trading $50k Intraday Eval.

Lucid's ACTUAL eval rules (NOT a cumulative lifetime loss limit):

+----------------------+----------+----------------------------------------+
| Rule                 | Value    | Behavior                               |
+----------------------+----------+----------------------------------------+
| Profit target        |  $3,000  | Reaching this passes the eval.         |
| Daily Loss Limit     |  $1,200  | SOFT breach — pauses trading for the   |
| (DLL)                |          | day. Resets each new trading day. Does |
|                      |          | NOT fail the account.                  |
| Max Loss Limit (MLL) | $2,000   | HARD breach — TRAILING drawdown from   |
|                      |          | the high-water mark (peak equity).     |
|                      |          | Account FAILS the moment equity drops  |
|                      |          | $2,000 below its peak (tracked live,   |
|                      |          | intraday).                             |
| Max contracts        | 4 minis  | Hard cap per symbol (4 NQ / 40 MNQ).   |
|                      | / 40     |                                        |
+----------------------+----------+--------------------------------------+

Sign convention
---------------
Positive PnL == profit. Drawdown is always measured relative to the
high-water mark.

Key concepts
------------
* High-water mark (HWM): the peak equity ever reached. The $2,000 Max Loss
  Limit is a shop from the HWM, so profits buy back "room" that is then
  protected.
* Daily P&L resets every new trading day: the day starts at $0 from the
  day's starting equity, and the $1,200 DLL applies to that day only.
  Hitting it is a SOFT Pause: trading pauses for the day, and the pause
  automatically clears on the next day's rollover.
* Two state machines (used by `check_order()` and exposed separately):
  - `is_daily_blocked()` -> DLL hit. Pauses trading until next day.
  - `is_hard_breach()`   -> trailing drawdown from HWM >= $2,000.
                           Account FAILED, permanently locked.
* `allow_live_trading: false` in config means this bot NEVER sends
  real-money orders; live mode is rejected in `check_order()`.
"""

import logging
from datetime import date, datetime, time
from pathlib import Path
from typing import Dict, List, Optional

import yaml

logger = logging.getLogger("RiskManager")

DEFAULT_CONFIG_PATH = str(
    Path(__file__).resolve().parent.parent / "config" / "risk_config.yaml"
)


class RiskManager:
    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        self.full_config = self._load_config(config_path)
        self.config = self.full_config.get("risk_limits", {})
        self.strategy_config = self.full_config.get("strategy", {})
        self.session_config = self.full_config.get("session", {})

        # Account identity
        self.account_size = 50000.0
        self.contract_multiplier = 20.0
        self.micro_contract_multiplier = self.config.get("micro_contract_multiplier", 2.0)
        
        # ── Lucid eval limits ──────────────
        self.profit_target = self.config.get("profit_target_usd", 3000.0)
        self.daily_loss_limit = self.config.get("daily_loss_limit_usd", 1200.0)
        self.trailing_drawdown_limit = self.config.get("trailing_drawdown_limit_usd", 2000.0)
        self.cumulative_loss_limit = self.trailing_drawdown_limit

        # ── Contract caps ──────────────────────────────────────────────────
        self.max_contracts = self.config.get("max_contracts", 4)
        self.max_contracts_micro = self.config.get("max_contracts_micro", 40)

        # ── Buffer thresholds ────────
        self.buffer_warning_pct = self.config.get("buffer_warning_pct", 50.0)
        self.buffer_critical_pct = self.config.get("buffer_critical_pct", 75.0)
        self.buffer_danger_pct = self.config.get("buffer_danger_pct", 90.0)

        # ── Trade constraints ─────────────────────────────────────────────
        self.max_trades_per_day = self.config.get("max_trades_per_day", 6)
        self.max_risk_per_trade_usd = self.config.get("max_risk_per_trade_usd", 500.0)
        self.max_consecutive_losses = self.config.get("max_consecutive_losses", 3)
        self.cooldown_after_losses = self.config.get("cooldown_after_losses", 3)

        # ── Cumulative P&L / high-water mark (never resets) ───────────────
        self.total_pnl = 0.0
        self.current_equity = self.account_size
        self.peak_equity = self.account_size
        self.max_drawdown = 0.0

        # ── Daily state ────────────────────────────────────────────────────
        self._current_date: Optional[date] = None
        self.day_start_pnl = 0.0
        self.day_start_equity = self.account_size
        self.daily_pnl = 0.0
        self.daily_pnl_history: List[float] = []
        self.trades_today = 0
        self.consecutive_losses = 0
        self.cooldown_remaining = 0
        self.daily_blocked = False

        # ── Hard-breach / kill switch ─────────────────────────────────────
        self.killed = False
        self.kill_reason = ""

        logger.info(
            f"RiskManager — Account: ${self.account_size:,.0f} | "
            f"Target: ${self.profit_target:,.0f} | "
            f"DLL: ${self.daily_loss_limit:,.0f} | "
            f"MLL: ${self.trailing_drawdown_limit:,.0f}"
        )

    def _load_config(self, path: str):
        try:
            with open(path, "r") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"Could not load config: {e}. Using defaults.")
            return {}

    def check_daily_reset(self, current_date: Optional[date] = None):
        if current_date is None:
            current_date = datetime.now().date()

        if self._current_date is None or current_date > self._current_date:
            if self._current_date is not None:
                self.daily_pnl_history.append(round(self.daily_pnl, 2))
            
            self.day_start_pnl = self.total_pnl
            self.day_start_equity = self.current_equity
            self.daily_pnl = 0.0
            self.trades_today = 0
            self.consecutive_losses = 0
            self.cooldown_remaining = 0
            self.daily_blocked = False
            self._current_date = current_date
            self.save_state()

    def _sync_daily(self):
        self.daily_pnl = self.total_pnl - self.day_start_pnl

    def get_trailing_drawdown(self) -> float:
        return max(0.0, self.peak_equity - self.current_equity)

    def get_mll_used_pct(self) -> float:
        return (self.get_trailing_drawdown() / self.trailing_drawdown_limit) * 100.0 if self.trailing_drawdown_limit > 0 else 0.0

    def is_hard_breach(self) -> bool:
        return self.get_trailing_drawdown() >= self.trailing_drawdown_limit

    def is_trading_session(self, current_time) -> bool:
        """Check if current time is within an allowed trading session.
        
        For paper trading, always returns True (trade anytime).
        For live trading, restricts to NY session windows.
        """
        if not self.config.get("session_restricted", False):
            return True
        from datetime import time as dtime
        sessions = [
            (dtime(9, 35), dtime(11, 30)),   # NY Morning
            (dtime(14, 0), dtime(16, 0)),     # NY Afternoon
        ]
        return any(start <= current_time <= end for start, end in sessions)

    def get_active_session(self, current_time) -> str:
        """Return the name of the active trading session, or 'OFF'."""
        from datetime import time as dtime
        if current_time and dtime(9, 35) <= current_time <= dtime(11, 30):
            return "NY_MORNING"
        elif current_time and dtime(14, 0) <= current_time <= dtime(16, 0):
            return "NY_AFTERNOON"
        return "OFF"

    def is_daily_blocked(self) -> bool:
        self.check_daily_reset()
        if not self.daily_blocked and self.daily_pnl <= -self.daily_loss_limit:
            self.daily_blocked = True
        return self.daily_blocked

    def check_order(self, quantity: int, current_positions_qty: int, mode: str) -> bool:
        # 1. Kill switch / hard breach — permanently locked.
        if self.killed or self.is_hard_breach():
            return False

        # 2. Live trading gate — config must explicitly allow it.
        if mode == "LIVE" and not self.config.get("allow_live_trading", False):
            return False

        # 3. Daily reset rollover, then daily soft-lock check.
        self.check_daily_reset()
        if self.is_daily_blocked():
            return False

        # 4. Buffer-scaled max for the current drawdown zone; 0 = no trading allowed.
        max_for_risk = self.get_max_contracts_for_current_risk()
        if max_for_risk == 0:
            return False

        # 5. Effective cap = min(config max, buffer-allowed). Reject over-sized totals.
        effective_max = min(self.max_contracts, max_for_risk)
        if abs(current_positions_qty + quantity) > effective_max:
            return False

        # 6. Daily trade-count limit.
        if self.trades_today >= self.max_trades_per_day:
            return False

        # 7. Daily loss cap (soft breach — pause for the day).
        if self.daily_pnl <= -self.daily_loss_limit:
            return False

        # 8. Cooldown after consecutive losses.
        if self.cooldown_remaining > 0:
            return False

        # 9. Cumulative / trailing-drawdown loss limit (hard).
        if self.total_pnl <= -self.cumulative_loss_limit:
            return False

        # 10. Risk-per-trade: worst-case USD risk at the current buffer-scaled stop.
        worst = abs(quantity) * self.get_adjusted_stop_loss() * self.contract_multiplier
        if worst > self.max_risk_per_trade_usd:
            return False
        if (self.total_pnl - worst) < -self.cumulative_loss_limit:
            return False

        return True

    def record_trade(self, pnl: float):
        self.total_pnl += pnl
        self.daily_pnl += pnl
        self.current_equity = self.account_size + self.total_pnl
        if self.current_equity > self.peak_equity:
            self.peak_equity = self.current_equity
        self.trades_today += 1
        
        # Check breaches
        self._sync_daily()
        self.is_daily_blocked()
        if self.is_hard_breach():
            self.killed = True
            self.kill_reason = "TRAILING_DRAWDOWN"

        # Persist risk state so a restart cannot silently clear kills / daily locks.
        self.save_state()

    def get_max_contracts_for_current_risk(self, symbol: str = "NQ") -> int:
        pct = self.get_mll_used_pct()
        if pct >= self.buffer_danger_pct:
            return 0
        if pct >= self.buffer_critical_pct:
            return 1
        if pct >= self.buffer_warning_pct:
            return max(1, self.max_contracts // 2)
        return self.max_contracts

    def get_adjusted_stop_loss(self) -> float:
        """Return stop-loss distance in points based on buffer zone."""
        pct = self.get_mll_used_pct()
        if pct >= self.buffer_danger_pct:
            return 10.0
        if pct >= self.buffer_critical_pct:
            return 15.0
        if pct >= self.buffer_warning_pct:
            return 20.0
        return 25.0

    def calculate_protective_stops(self, entry_price: float, side: str) -> tuple:
        """Calculate stop-loss and take-profit prices for a bracket order.

        Returns:
            (stop_loss, take_profit) as float prices.
        """
        sl_pts = self.get_adjusted_stop_loss()
        tp_pts = sl_pts * 2.0  # 2:1 reward-to-risk

        if side.upper() == "BUY":
            stop_loss = round(entry_price - sl_pts, 2)
            take_profit = round(entry_price + tp_pts, 2)
        else:
            stop_loss = round(entry_price + sl_pts, 2)
            take_profit = round(entry_price - tp_pts, 2)

        return stop_loss, take_profit

    # ------------------------------------------------------------------
    # Persistence (B8) — risk state survives restarts
    # ------------------------------------------------------------------

    def save_state(self, db=None):
        """Persist risk state so a restart cannot clear kills / daily locks.

        Degrades gracefully: if the DB is unavailable or read-only we only
        warn and keep running with in-memory state.
        """
        from backend.database import SessionLocal, DBRiskState, init_db
        own_session = db is None
        session = None
        try:
            init_db()  # idempotent — ensures the risk_state table exists
            session = db if db is not None else SessionLocal()
            row = session.query(DBRiskState).first()
            if row is None:
                row = DBRiskState()
                session.add(row)
            row.account_size = self.account_size
            row.total_pnl = self.total_pnl
            row.peak_equity = self.peak_equity
            row.daily_pnl = self.daily_pnl
            row.day_start_pnl = self.day_start_pnl
            row.current_date = self._current_date.isoformat() if self._current_date else None
            row.trades_today = self.trades_today
            row.consecutive_losses = self.consecutive_losses
            row.cooldown_remaining = self.cooldown_remaining
            row.daily_blocked = self.daily_blocked
            row.killed = self.killed
            row.kill_reason = self.kill_reason
            row.updated_at = datetime.now()
            session.commit()
        except Exception as e:
            logger.warning(f"RiskManager: could not persist risk state: {e}")
        finally:
            if own_session and session is not None:
                session.close()

    def load_state(self, db=None) -> bool:
        """Restore persisted risk state after a restart.

        Returns False (and keeps fresh defaults) if no state row exists or
        the DB is unavailable — never raises.
        """
        from backend.database import SessionLocal, DBRiskState, init_db
        own_session = db is None
        session = None
        try:
            init_db()  # idempotent — ensures the risk_state table exists
            session = db if db is not None else SessionLocal()
            row = session.query(DBRiskState).first()
            if row is None:
                return False
            self.account_size = float(row.account_size or 50000.0)
            self.total_pnl = float(row.total_pnl or 0.0)
            self.peak_equity = float(row.peak_equity or self.account_size)
            self.day_start_pnl = float(row.day_start_pnl or 0.0)
            self._current_date = (
                date.fromisoformat(row.current_date) if row.current_date else None
            )
            self.trades_today = int(row.trades_today or 0)
            self.consecutive_losses = int(row.consecutive_losses or 0)
            self.cooldown_remaining = int(row.cooldown_remaining or 0)
            self.daily_blocked = bool(row.daily_blocked)
            self.killed = bool(row.killed)
            self.kill_reason = row.kill_reason or ""
            self.current_equity = self.account_size + self.total_pnl
            self.daily_pnl = self.total_pnl - self.day_start_pnl
            logger.info(
                f"RiskManager loaded persisted state: total_pnl=${self.total_pnl:,.2f} "
                f"killed={self.killed} trades_today={self.trades_today}"
            )
            return True
        except Exception as e:
            logger.warning(f"RiskManager: could not load persisted state ({e}); starting fresh.")
            return False
        finally:
            if own_session and session is not None:
                session.close()

    # ------------------------------------------------------------------
    # Eval progress / consistency helpers (used by dashboard & tests)
    # ------------------------------------------------------------------

    def check_consistency(self) -> dict:
        """Consistency rule: no single day > max_day_pct % of total profits."""
        max_day_pct = self.config.get("max_day_pct", 30.0)
        if self.total_pnl <= 0 or not self.daily_pnl_history:
            return {
                "compliant": True,
                "max_single_day_pct": 0.0,
                "max_day_pct": max_day_pct,
            }
        max_day = max(self.daily_pnl_history)
        max_single_day_pct = (max_day / self.total_pnl) * 100.0
        return {
            "compliant": bool(max_single_day_pct < max_day_pct),
            "max_single_day_pct": round(max_single_day_pct, 2),
            "max_day_pct": max_day_pct,
        }

    def get_eval_progress(self) -> dict:
        """Return an eval-progress snapshot (aligned with EvalProgress schema)."""
        self.check_daily_reset()
        self._sync_daily()
        mll_used = self.get_mll_used_pct()
        if self.killed:
            status = "KILLED"
        elif self.is_daily_blocked():
            status = "BLOCKED"
        else:
            status = "TRADING"
        return {
            "status": status,
            "kill_reason": self.kill_reason,
            "account_size": self.account_size,
            "current_equity": self.current_equity,
            "total_pnl": self.total_pnl,
            "daily_pnl": self.daily_pnl,
            "profit_target": self.profit_target,
            "profit_progress_pct": round(
                max(0.0, min(100.0, (self.total_pnl / self.profit_target) * 100.0))
            ) if self.profit_target else 0.0,
            "daily_loss_limit": self.daily_loss_limit,
            "daily_loss_used_pct": round(mll_used, 2),
            "intraday_drawdown": self.get_trailing_drawdown(),
            "intraday_drawdown_limit": self.trailing_drawdown_limit,
            "consistency": self.check_consistency(),
            "trades_today": self.trades_today,
            "max_trades_today": self.max_trades_per_day,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_remaining": self.cooldown_remaining,
            "days_traded": len(self.daily_pnl_history),
        }

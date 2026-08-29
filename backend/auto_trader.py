"""
Auto-Trader: Fetches real NQ data and runs the momentum strategy automatically.

Targets $60,000 equity. Hard stop at $49,500 — kills all trading if breached.
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, time as dtime
from typing import Optional

logger = logging.getLogger("AutoTrader")


class AutoTrader:
    """
    Background auto-trader that:
    1. Fetches real NQ=F hourly data from Yahoo Finance
    2. Feeds prices through NQMomentumStrategy.on_tick()
    3. Executes paper trades via PaperTradingState
    4. Enforces hard stop ($49,500) and profit target ($60,000)
    """

    HARD_STOP_EQUITY = 49500.0
    PROFIT_TARGET_EQUITY = 60000.0
    ACCOUNT_SIZE = 50000.0
    CONTRACT_MULTIPLIER = 20.0
    # How often to check for new data (seconds)
    POLL_INTERVAL = 60
    # Only trade during NY session hours (Mountain Time -> adjust)
    NY_OPEN = dtime(7, 35)   # 9:35 ET = 7:35 MT
    NY_CLOSE = dtime(14, 0)  # 16:00 ET = 14:00 MT
    NY_AFTERNOON_OPEN = dtime(12, 0)  # 14:00 ET = 12:00 MT

    def __init__(self, strategy, paper_state, risk_mgr):
        self.strategy = strategy
        self.paper_state = paper_state
        self.risk_mgr = risk_mgr
        self.running = False
        self.stopped_reason = None
        self._task: Optional[asyncio.Task] = None
        self._last_price: Optional[float] = None
        self._bars_fed = 0
        self._trades_executed = 0
        # Eval-derived exit levels, from the risk config (not hardcoded).
        # The class attrs below remain as safe defaults.
        self.profit_target_equity = self.risk_mgr.account_size + self.risk_mgr.profit_target
        self.hard_stop_equity = self.risk_mgr.account_size - self.risk_mgr.trailing_drawdown_limit

    def start(self):
        """Start the auto-trader background loop."""
        if self.running:
            return {"status": "already_running"}
        self.running = True
        self.stopped_reason = None
        self._task = asyncio.create_task(self._run_loop())
        logger.info("AutoTrader STARTED — targeting $60,000, hard stop at $49,500")
        return {"status": "started"}

    def stop(self, reason: str = "manual"):
        """Stop the auto-trader."""
        self.running = False
        self.stopped_reason = reason
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info(f"AutoTrader STOPPED — reason: {reason}")
        return {"status": "stopped", "reason": reason}

    def get_status(self) -> dict:
        equity = self.paper_state.equity
        return {
            "auto_trading": self.running,
            "stopped_reason": self.stopped_reason,
            "equity": equity,
            "hard_stop": self.hard_stop_equity,
            "profit_target": self.profit_target_equity,
            "bars_fed": self._bars_fed,
            "trades_executed": self._trades_executed,
            "position_qty": self.strategy.position_qty,
            "position_side": self.strategy.position_side,
            "last_price": self._last_price,
        }

    async def _run_loop(self):
        """Main loop — fetch data, feed strategy, execute trades."""
        from backend.data_provider import MarketDataProvider
        provider = MarketDataProvider()

        logger.info("AutoTrader loop started — fetching NQ=F data every 60s")

        # ── Pre-load historical data so SMA/RSI have enough history ──
        seeded = False
        while self.running and not seeded:
            try:
                hourly = provider.fetch_nq_hourly(period="5d")
                if hourly and len(hourly) >= 25:
                    for bar in hourly:
                        p = float(bar["close"])
                        self._last_price = p
                        self._bars_fed += 1
                        self.strategy.on_tick(p, int(time.time()), datetime.now().time())
                    seeded = True
                    logger.info(f"AutoTrader: seeded strategy with {len(hourly)} historical bars, last={self._last_price}")
                else:
                    logger.warning(f"AutoTrader: only got {len(hourly) if hourly else 0} bars, retrying...")
                    await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"AutoTrader seed error: {e}")
                await asyncio.sleep(10)

        # ── Main trading loop — poll for new bars ──
        seen_bars = set()
        while self.running:
            try:
                # Equity including unrealized P&L at the last known price.
                current_equity = self.paper_state.get_state(
                    current_price=self._last_price
                )["equity"]

                # ── Check hard stop ──────────────────────────────
                if current_equity <= self.hard_stop_equity:
                    self.stop(f"HARD STOP HIT: equity ${current_equity:,.2f} <= ${self.hard_stop_equity:,.2f}")
                    if self.strategy.position_qty != 0:
                        self._flatten_position(self._last_price or 18500.0, "Hard stop breach")
                    break

                # ── Check profit target ──────────────────────────
                if current_equity >= self.profit_target_equity:
                    self.stop(f"PROFIT TARGET HIT: equity ${current_equity:,.2f} >= ${self.profit_target_equity:,.2f}")
                    if self.strategy.position_qty != 0:
                        self._flatten_position(self._last_price or 18500.0, "Profit target reached")
                    break

                # ── Fetch real NQ data ───────────────────────────
                hourly = provider.fetch_nq_hourly(period="5d")
                if not hourly or len(hourly) < 25:
                    logger.warning("AutoTrader: insufficient data, retrying in 60s")
                    await asyncio.sleep(self.POLL_INTERVAL)
                    continue

                # Only feed NEW bars (not ones we already processed)
                new_bars = 0
                for bar in hourly:
                    bar_key = bar.get("date", "")
                    if bar_key not in seen_bars:
                        seen_bars.add(bar_key)
                        p = float(bar["close"])
                        self._last_price = p
                        self._bars_fed += 1
                        new_bars += 1

                        now = datetime.now().time()
                        signal = self.strategy.on_tick(
                            price=p,
                            timestamp=int(time.time()),
                            current_time=now,
                        )
                        if signal:
                            self._execute_signal(signal, p, bar)

                if new_bars > 0:
                    logger.info(
                        f"AutoTrader: {new_bars} new bars, price={self._last_price:.2f} "
                        f"equity=${self.paper_state.equity:,.2f} "
                        f"pos={self.strategy.position_qty}"
                    )

                # ── Protective stop monitor (SL/TP set at open) ──
                self._check_protective_stop()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"AutoTrader error: {e}")

            await asyncio.sleep(self.POLL_INTERVAL)

        logger.info("AutoTrader loop ended")

    def _execute_signal(self, signal: dict, price: float, bar_data: dict):
        """Execute a strategy signal through the paper trading state."""
        action = signal["action"]
        qty = signal.get("quantity", 1)
        symbol = "NQ"
        is_exit = signal.get("exit", False)

        if action == "FLATTEN":
            self._flatten_position(price, signal.get("reason", "flattened"))
            return

        # Record the trade in paper state (this opens or closes position)
        pnl = self.paper_state.record_trade(
            symbol=symbol,
            side=action,
            qty=qty,
            price=price,
            current_date=datetime.now().date(),
        )

        # If this is an exit (close) signal, reset strategy position
        if is_exit or self.paper_state.open_positions.get(symbol) is None:
            self.strategy.position_qty = 0
            self.strategy.position_side = None
            self.strategy.entry_price = 0.0
            self.strategy.trailing_stop = 0.0
            self.strategy.stop_loss = 0.0
            self.strategy.take_profit = 0.0
            self.strategy.bars_in_position = 0
            self.strategy.bars_since_last_trade = 0
        else:
            # Opening/increasing position
            if action == "BUY":
                self.strategy.position_qty = self.paper_state.open_positions[symbol]["qty"]
                self.strategy.position_side = "LONG"
            else:
                self.strategy.position_qty = -self.paper_state.open_positions[symbol]["qty"]
                self.strategy.position_side = "SHORT"
            self.strategy.entry_price = self.paper_state.open_positions[symbol]["entry_price"]
            self.strategy.trailing_stop = signal.get("stop_loss", price)
            self.strategy.stop_loss = signal.get("stop_loss", 0.0) or 0.0
            self.strategy.take_profit = signal.get("take_profit", 0.0) or 0.0
            self.strategy.bars_in_position = 0
            self.strategy.bars_since_last_trade = 0

        # Sync risk manager
        self.risk_mgr.record_trade(pnl)

        # Record trade to DB so trades tab and dashboard counts stay in sync
        self._record_db_trade(action, qty, symbol, price, signal.get("reason", "auto-trade"))

        self._trades_executed += 1
        logger.info(
            f"AutoTrader TRADE #{self._trades_executed}: "
            f"{action} {qty} {symbol} @ {price:.2f} PnL=${pnl:,.2f} "
            f"equity=${self.paper_state.equity:,.2f} "
            f"pos={self.strategy.position_qty}"
        )

    def _flatten_position(self, price: float, reason: str):
        """Close all open positions. PaperTradingState is the authority."""
        pos = self.paper_state.open_positions.get("NQ")
        if pos is None or pos.get("qty", 0) == 0:
            # No real paper position — fall back to strategy qty for the DB record only.
            qty = abs(self.strategy.position_qty)
            if qty == 0:
                return
            side = "SELL" if self.strategy.position_qty > 0 else "BUY"
            self._record_db_trade(side, qty, "NQ", price, reason)
        else:
            qty = pos["qty"]
            side = "SELL" if pos["side"] == "LONG" else "BUY"
            pnl = self.paper_state.record_trade(
                symbol="NQ", side=side, qty=qty, price=price,
                current_date=datetime.now().date(),
            )
            self.risk_mgr.record_trade(pnl)
            # Record trade to DB
            self._record_db_trade(side, qty, "NQ", price, reason)
        # Reset strategy state
        self.strategy.position_qty = 0
        self.strategy.position_side = None
        self.strategy.entry_price = 0.0
        self.strategy.trailing_stop = 0.0
        self.strategy.stop_loss = 0.0
        self.strategy.take_profit = 0.0
        self.strategy.bars_in_position = 0
        self._trades_executed += 1
        logger.info(f"AutoTrader FLATTEN: {qty} NQ @ {price:.2f} PnL=$0.00 reason={reason}")

    def _check_protective_stop(self):
        """Flatten if the last price crosses the stored SL/TP for the open position."""
        if self.strategy.position_qty == 0 or self._last_price is None:
            return
        stop = getattr(self.strategy, "stop_loss", 0.0) or 0.0
        target = getattr(self.strategy, "take_profit", 0.0) or 0.0
        if stop == 0.0 and target == 0.0:
            return
        crossed = None
        if self.strategy.position_side == "LONG":
            if stop and self._last_price <= stop:
                crossed = "STOP_LOSS"
            elif target and self._last_price >= target:
                crossed = "TAKE_PROFIT"
        elif self.strategy.position_side == "SHORT":
            if stop and self._last_price >= stop:
                crossed = "STOP_LOSS"
            elif target and self._last_price <= target:
                crossed = "TAKE_PROFIT"
        if crossed:
            logger.warning(
                f"AutoTrader protective stop: {crossed} "
                f"price={self._last_price:.2f} for pos={self.strategy.position_side} "
                f"(SL={stop}, TP={target})"
            )
            self._flatten_position(self._last_price, crossed)

    def _record_db_trade(self, action, qty, symbol, price, reason):
        """Write a DBTrade row so the trades tab and dashboard counts work."""
        from backend.database import SessionLocal, DBTrade
        db = SessionLocal()
        try:
            trade = DBTrade(
                trade_id=f"auto_{uuid.uuid4().hex[:12]}",
                symbol=symbol,
                side=action,
                quantity=qty,
                price=price,
                reason=reason,
                order_type="MARKET",
            )
            db.add(trade)
            db.commit()
        except Exception as e:
            logger.error(f"AutoTrader: failed to record DBTrade: {e}")
            db.rollback()
        finally:
            db.close()

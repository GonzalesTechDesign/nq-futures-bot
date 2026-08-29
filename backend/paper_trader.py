"""
Paper Trading State — simulates account equity, positions, and P&L
for the paper trading dashboard.

The dashboard reads from DBPnLRecord, but this module is the source of
truth during a live session.  Each trade execution calls record_trade(),
which updates equity / P&L / position tracking and returns the realized
P&L for that leg.  A DBPnLRecord is then written so the dashboard's
DB-backed read path stays current across page reloads.
"""

import logging
from datetime import date
from typing import Dict, Optional

logger = logging.getLogger("PaperTrader")


class PaperTradingState:
    """
    Manages the simulated paper trading account state.

    Tracks equity, open positions (with entry prices), realized P&L,
    win/loss statistics, and max drawdown so the dashboard shows real
    numbers derived from actual trade execution — never hardcoded.
    """

    ACCOUNT_SIZE = 50000.0
    CONTRACT_MULTIPLIER = 20.0  # NQ = $20 per point

    def __init__(self):
        self.equity: float = self.ACCOUNT_SIZE
        self.peak_equity: float = self.ACCOUNT_SIZE
        self.total_pnl: float = 0.0
        self.daily_pnl: float = 0.0
        self.trades_today: int = 0
        self.wins: int = 0
        self.losses: int = 0
        self.current_date: Optional[date] = None
        self.open_positions: Dict[str, dict] = {}  # symbol -> {qty, side, entry_price}

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def record_trade(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        current_date: Optional[date] = None,
    ) -> float:
        """
        Record a trade execution.  If it closes or reduces an open
        position, realize P&L.  If it opens or adds to a position,
        return 0.0.

        Parameters
        ----------
        symbol : str
            Contract symbol, e.g. "NQ" or "NQU6".
        side : str
            "BUY" or "SELL".
        qty : int
            Number of contracts.
        price : float
            Fill price.
        current_date : date, optional
            Trading date for daily-reset logic.  Defaults to today.

        Returns
        -------
        float
            Realized P&L for this trade in USD (0.0 when opening/adding).
        """
        pnl = 0.0

        # Reset daily state on a new trading day
        if current_date is None:
            current_date = date.today()
        if current_date != self.current_date:
            self.daily_pnl = 0.0
            self.trades_today = 0
            self.current_date = current_date

        pos = self.open_positions.get(symbol)
        side_upper = side.upper()

        if pos is None or pos["qty"] == 0:
            # ── Opening a new position ──────────────────────────────
            self.open_positions[symbol] = {
                "qty": qty,
                "side": "LONG" if side_upper == "BUY" else "SHORT",
                "entry_price": price,
            }
            logger.debug(
                f"PaperTrader: Opened {side_upper} {qty} {symbol} @ {price}"
            )
        else:
            # ── Existing position: close/reduce or add? ─────────────
            is_closing = (
                (pos["side"] == "LONG" and side_upper == "SELL")
                or (pos["side"] == "SHORT" and side_upper == "BUY")
            )

            if is_closing:
                close_qty = min(qty, pos["qty"])
                if pos["side"] == "LONG":
                    pnl = (price - pos["entry_price"]) * close_qty * self.CONTRACT_MULTIPLIER
                else:
                    pnl = (pos["entry_price"] - price) * close_qty * self.CONTRACT_MULTIPLIER

                self.total_pnl += pnl
                self.daily_pnl += pnl
                self.trades_today += 1

                if pnl >= 0:
                    self.wins += 1
                else:
                    self.losses += 1

                pos["qty"] -= close_qty
                if pos["qty"] == 0:
                    del self.open_positions[symbol]

                logger.info(
                    f"PaperTrader: Closed {close_qty} {symbol} @ {price} "
                    f"P&L=${pnl:+.2f} (daily=${self.daily_pnl:+.2f}, "
                    f"total=${self.total_pnl:+.2f})"
                )
            else:
                # ── Adding to position — average entry price ────────
                total_qty = pos["qty"] + qty
                pos["entry_price"] = (
                    (pos["entry_price"] * pos["qty"] + price * qty) / total_qty
                )
                pos["qty"] = total_qty
                pos["side"] = "LONG" if side_upper == "BUY" else "SHORT"
                logger.debug(
                    f"PaperTrader: Added {qty} {symbol} — now "
                    f"{pos['qty']} @ avg {pos['entry_price']:.2f}"
                )

        # ── Update equity & high-water mark ─────────────────────────
        self.equity = self.ACCOUNT_SIZE + self.total_pnl
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

        return pnl

    # ------------------------------------------------------------------
    # Derived metrics
    # ------------------------------------------------------------------

    def update_unrealized(self, prices: Dict[str, float]):
        """Update equity with unrealized P&L from current market prices.

        Call this on every dashboard refresh / WS broadcast tick.
        ``prices`` is a dict of {symbol: current_price}, e.g. {"NQ": 29548.25}.
        """
        unrealized = 0.0
        for sym, pos in self.open_positions.items():
            current = prices.get(sym)
            if current is None:
                continue
            if pos["side"] == "LONG":
                unrealized += (current - pos["entry_price"]) * pos["qty"] * self.CONTRACT_MULTIPLIER
            else:
                unrealized += (pos["entry_price"] - current) * pos["qty"] * self.CONTRACT_MULTIPLIER
        self._unrealized_pnl = unrealized

    @property
    def equity_with_unrealized(self) -> float:
        """Total equity including unrealized P&L."""
        return self.ACCOUNT_SIZE + self.total_pnl + getattr(self, '_unrealized_pnl', 0.0)

    def get_max_drawdown(self) -> float:
        """Max drawdown as a negative number (e.g. -500.0), or 0.0."""
        dd = self.peak_equity - self.equity
        return -dd if dd > 0 else 0.0

    @property
    def win_rate(self) -> float:
        """Win rate as a fraction 0.0–1.0."""
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.0

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    def get_state(self, current_price: Optional[float] = None) -> dict:
        """Return the full paper trading state for dashboard consumption."""
        positions = {}
        for sym, pos in self.open_positions.items():
            positions[sym] = dict(pos)
            if current_price is not None:
                # Unrealized P&L = (current_price - entry) * qty * multiplier
                # For SHORT, entry - current_price
                if pos["side"] == "LONG":
                    unrealized = (current_price - pos["entry_price"]) * pos["qty"] * self.CONTRACT_MULTIPLIER
                else:
                    unrealized = (pos["entry_price"] - current_price) * pos["qty"] * self.CONTRACT_MULTIPLIER
                positions[sym]["unrealized_pnl"] = round(unrealized, 2)
            else:
                positions[sym]["unrealized_pnl"] = 0.0
                
        return {
            "equity": self.equity + sum(p.get("unrealized_pnl", 0.0) for p in positions.values()),
            "peak_equity": self.peak_equity,
            "total_pnl": self.total_pnl,
            "daily_pnl": self.daily_pnl,
            "trades_today": self.trades_today,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": self.win_rate,
            "max_drawdown": self.get_max_drawdown(),
            "open_positions": positions,
        }

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def reset(self):
        """Reset the paper trading account to its initial state."""
        self.equity = self.ACCOUNT_SIZE
        self.peak_equity = self.ACCOUNT_SIZE
        self.total_pnl = 0.0
        self.daily_pnl = 0.0
        self.trades_today = 0
        self.wins = 0
        self.losses = 0
        self.current_date = None
        self.open_positions = {}
        logger.info("PaperTrader: Account reset to $50,000.")

import logging
from typing import Dict, List, Any, Optional
from backend.ibkr_client import IBKRClient

logger = logging.getLogger("BrokerReconciliation")

class BrokerReconciliation:
    """
    Performs reconciliation between the intended paper-trading state and live
    broker (IBKR) positions and fills.

    The paper-trading state (PaperTradingState.open_positions) is the source
    of truth for what the bot believes it holds; DBPosition rows are not
    written by the execution path, so we do NOT diff against the DB here.
    """
    def __init__(self, ibkr_client: IBKRClient):
        self.ibkr_client = ibkr_client

    def reconcile_positions(self, paper_positions: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
        """
        Reconcile intended positions against broker positions.

        Args:
            paper_positions: dict {symbol: signed quantity} built from
                PaperTradingState.open_positions (positive = long,
                negative = short). When omitted, falls back to the legacy
                DBPosition rows (which are typically empty).

        Returns a payload documenting the source used.
        """
        logger.info("Starting broker position reconciliation...")
        try:
            broker_positions = self.ibkr_client.get_positions()
        except Exception as e:
            logger.error(f"Reconciliation failed to fetch broker positions: {e}")
            return {"status": "ERROR", "discrepancies": str(e)}

        if paper_positions is not None:
            source = "paper_state"
            intended_map = dict(paper_positions)
        else:
            from backend.database import SessionLocal, DBPosition
            source = "db"
            db = SessionLocal()
            try:
                db_positions = db.query(DBPosition).all()
                intended_map = {p.symbol: p.quantity for p in db_positions}
            finally:
                db.close()

        broker_pos_map = {p["symbol"]: p["quantity"] for p in broker_positions}

        discrepancies = []
        all_symbols = set(intended_map.keys()).union(set(broker_pos_map.keys()))

        for sym in all_symbols:
            intended_qty = intended_map.get(sym, 0)
            broker_qty = broker_pos_map.get(sym, 0)
            if intended_qty != broker_qty:
                discrepancies.append({
                    "symbol": sym,
                    "intended_quantity": intended_qty,
                    "broker_quantity": broker_qty
                })
                logger.warning(
                    f"RECONCILIATION DISCREPANCY on {sym}: "
                    f"paper={intended_qty} vs IBKR={broker_qty}"
                )
            else:
                logger.info(f"Reconciliation matched for {sym}: Qty={broker_qty}")

        status = "OK" if not discrepancies else "DISCREPANCY_DETECTED"
        return {
            "status": status,
            "discrepancies": discrepancies,
            "broker_positions": broker_positions,
            "source": source,
            "method": "paper_state_vs_broker" if source == "paper_state" else "db_vs_broker",
        }

import logging
from typing import Dict, List, Any
from backend.ibkr_client import IBKRClient
from backend.database import SessionLocal, DBPosition

logger = logging.getLogger("BrokerReconciliation")

class BrokerReconciliation:
    """
    Performs reconciliation between local database state and live broker (IBKR) positions and fills.
    """
    def __init__(self, ibkr_client: IBKRClient):
        self.ibkr_client = ibkr_client

    def reconcile_positions(self) -> Dict[str, Any]:
        logger.info("Starting broker position reconciliation...")
        try:
            broker_positions = self.ibkr_client.get_positions()
        except Exception as e:
            logger.error(f"Reconciliation failed to fetch broker positions: {e}")
            return {"status": "ERROR", "discrepancies": str(e)}

        db = SessionLocal()
        try:
            db_positions = db.query(DBPosition).all()
            db_pos_map = {p.symbol: p.quantity for p in db_positions}
            broker_pos_map = {p["symbol"]: p["quantity"] for p in broker_positions}

            discrepancies = []
            all_symbols = set(db_pos_map.keys()).union(set(broker_pos_map.keys()))

            for sym in all_symbols:
                db_qty = db_pos_map.get(sym, 0)
                broker_qty = broker_pos_map.get(sym, 0)
                if db_qty != broker_qty:
                    discrepancies.append({
                        "symbol": sym,
                        "db_quantity": db_qty,
                        "broker_quantity": broker_qty
                    })
                    logger.warning(f"RECONCILIATION DISCREPANCY on {sym}: DB Qty={db_qty} vs IBKR Qty={broker_qty}")
                else:
                    logger.info(f"Reconciliation matched for {sym}: Qty={broker_qty}")

            status = "OK" if not discrepancies else "DISCREPANCY_DETECTED"
            return {
                "status": status,
                "discrepancies": discrepancies,
                "broker_positions": broker_positions
            }
        finally:
            db.close()

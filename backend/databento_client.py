import os
import logging
from typing import Optional, List, Dict
import databento as db
from backend.circuit_breaker import CircuitBreaker, with_circuit_breaker

logger = logging.getLogger("DatabentoClient")

class DatabentoClient:
    """
    Production-grade Databento client for CME GLBX.MDP3 feed supporting NQ1! and MNQ1!
    """
    def __init__(self):
        self.api_key = os.getenv("DATABENTO_API_KEY", "")
        self.client = None
        self.breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=15.0)
        if self.api_key and self.api_key != "db-your-databento-api-key-here":
            try:
                self.client = db.Historical(key=self.api_key)
                logger.info("Databento historical client initialized successfully.")
            except Exception as e:
                logger.error(f"Failed to initialize Databento client: {e}")

    @with_circuit_breaker(lambda self: self.breaker)
    def fetch_historical_trades(self, symbol: str = "NQ.FUT", start: str = "2026-01-01", end: str = "2026-01-07") -> List[Dict]:
        if not self.client:
            logger.warning("Databento client not configured with valid API key. Returning simulated sample data for testing.")
            return [
                {"timestamp": "2026-01-06T09:30:00Z", "symbol": symbol, "price": 18500.0, "size": 5},
                {"timestamp": "2026-01-06T09:30:01Z", "symbol": symbol, "price": 18502.5, "size": 2}
            ]
        
        try:
            data = self.client.timeseries.get_range(
                dataset="GLBX.MDP3",
                stype_in="continuous",
                stype_out="instrument_id",
                symbols=[symbol],
                schema="trades",
                start=start,
                end=end
            )
            df = data.to_df()
            records = []
            for _, row in df.iterrows():
                records.append({
                    "timestamp": str(row.get("ts_event", "")),
                    "symbol": symbol,
                    "price": float(row.get("price", 0.0)) / 1e9 if "price" in row else 0.0,
                    "size": int(row.get("size", 0))
                })
            return records
        except Exception as e:
            logger.error(f"Databento fetch error: {e}")
            raise e

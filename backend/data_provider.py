"""
Market data provider for NQ futures.
Supports Yahoo Finance (free) with fallback to synthetic generation.
"""

import logging
from typing import List, Optional
import numpy as np

logger = logging.getLogger("DataProvider")


class MarketDataProvider:
    """Fetches real NQ futures OHLCV data."""

    def __init__(self):
        self._cache = {}

    def fetch_nq_daily(self, period: str = "6mo") -> Optional[List[dict]]:
        """Fetch NQ=F daily OHLCV from Yahoo Finance."""
        try:
            import yfinance as yf
            ticker = yf.Ticker("NQ=F")
            df = ticker.history(period=period, interval="1d")
            if df.empty:
                logger.warning("No daily data returned from Yahoo Finance")
                return None

            records = []
            for idx, row in df.iterrows():
                records.append({
                    "date": str(idx.date()),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(row["Volume"]),
                })

            logger.info(f"Fetched {len(records)} daily bars for NQ=F ({records[0]['date']} to {records[-1]['date']})")
            return records
        except Exception as e:
            logger.error(f"Failed to fetch NQ daily data: {e}")
            return None

    def fetch_nq_hourly(self, period: str = "1mo") -> Optional[List[dict]]:
        """Fetch NQ=F hourly OHLCV from Yahoo Finance."""
        try:
            import yfinance as yf
            ticker = yf.Ticker("NQ=F")
            df = ticker.history(period=period, interval="1h")
            if df.empty:
                logger.warning("No hourly data returned from Yahoo Finance")
                return None

            records = []
            for idx, row in df.iterrows():
                records.append({
                    "date": str(idx),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(row["Volume"]),
                })

            logger.info(f"Fetched {len(records)} hourly bars for NQ=F")
            return records
        except Exception as e:
            logger.error(f"Failed to fetch NQ hourly data: {e}")
            return None

    def daily_to_minute_bars(self, daily_records: List[dict]) -> List[np.ndarray]:
        """
        Convert daily OHLCV to synthetic intraday minute bars.
        Each daily bar becomes 390 minute bars (6.5 hour session).
        Uses realistic intraday patterns based on OHLC.
        """
        daily_bars = []
        rng = np.random.default_rng(42)

        for day in daily_records:
            bars = np.zeros(390)
            o, h, l, c = day["open"], day["high"], day["low"], day["close"]

            # Guard against NaN/missing OHLC rows from the upstream feed.
            # Use price=last valid close or the open; if all invalid, skip the day.
            if None in (o, h, l, c) or any(np.isnan(np.array([o, h, l, c], dtype=float))):
                logger.warning("Skipping day with invalid OHLC: %s", day.get("date"))
                continue
            o, h, l, c = float(o), float(h), float(l), float(c)

            # Determine trend direction for the day
            bullish = c > o

            # Generate intraday path from O -> H/L -> C
            price = o
            for bar in range(390):
                # Intraday volatility profile
                if bar < 35:  # pre-market
                    vol_scale = 0.6
                elif bar < 50:  # first 15 min — big moves
                    vol_scale = 1.8
                elif bar < 150:  # morning session
                    vol_scale = 1.2
                elif bar < 270:  # lunch
                    vol_scale = 0.7
                elif bar < 390:  # afternoon
                    vol_scale = 1.0
                else:
                    vol_scale = 0.5

                # Drift toward the close as the day progresses
                progress = bar / 390.0
                drift_weight = 0.001 * progress
                target = c
                drift = drift_weight * (target - price) / max(abs(target - price), 1)

                # Volatility
                daily_range = h - l
                bar_vol = daily_range / 390.0 * vol_scale
                noise = rng.normal(0, bar_vol)

                price = price + drift * price + noise
                price = max(l, min(h, price))  # stay within day's range
                bars[bar] = round(price, 2)

            daily_bars.append(bars)

        return daily_bars

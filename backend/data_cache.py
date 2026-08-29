"""
Immutable, append-only OHLCV disk cache for NQ futures market data.

The self-improving agent harness needs reproducible offline tuning on REAL
history. Live Yahoo fetches are mutable and network-dependent, so we cache
every bar ever observed to disk, keyed by (symbol, interval). Once a bar's
date is written it is never rewritten or deleted — this guarantees a
tuning run always sees the same data for the same date range.

Design:
- One CSV per (symbol, interval), e.g. data/ohlcv/NQ=F_1d.csv
- Append-only: a bar is only added if its date isn't already present.
- A tiny JSON index file caches the sorted set of dates for fast range checks
  (regenerated on write; no complex locking for a single-writer process).
- datetime-string aware so hourly/daily granularities both work.

This deliberately avoids heavy deps (no pandas/parquet requirement for the
hot path) and keeps the data format plain and diffable.
"""

import csv
import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("DataCache")


class OHLCVCache:
    def __init__(self, cache_dir: Optional[str] = None):
        project_root = Path(__file__).resolve().parent.parent
        self.cache_dir = Path(cache_dir) if cache_dir else project_root / "data" / "ohlcv"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ── path helpers ──────────────────────────────────────────────────────────
    def _csv_path(self, symbol: str, interval: str) -> Path:
        safe = symbol.replace("/", "_").replace("=", "_")
        return self.cache_dir / f"{safe}_{interval}.csv"

    def _index_path(self, symbol: str, interval: str) -> Path:
        return self._csv_path(symbol, interval).with_suffix(".json")

    # ── reads ─────────────────────────────────────────────────────────────────
    def load(self, symbol: str, interval: str) -> List[Dict]:
        """Return all cached rows for (symbol, interval), oldest first."""
        path = self._csv_path(symbol, interval)
        if not path.exists():
            return []
        rows = []
        with open(path, "r", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append({
                    "date": r["date"],
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": int(float(r["volume"])),
                })
        rows.sort(key=lambda r: r["date"])
        return rows

    def available_dates(self, symbol: str, interval: str) -> set:
        """Return the set of dates currently cached (fast via index if present)."""
        idx = self._index_path(symbol, interval)
        if idx.exists():
            try:
                return set(json.loads(idx.read_text()))
            except Exception:
                pass
        dates = {r["date"] for r in self.load(symbol, interval)}
        try:
            idx.write_text(json.dumps(sorted(dates)))
        except Exception:
            pass
        return dates

    def load_range(self, symbol: str, interval: str, start: Optional[str] = None,
                   end: Optional[str] = None) -> List[Dict]:
        """Return cached rows filtered to [start, end] (inclusive, by string date)."""
        rows = self.load(symbol, interval)
        out = []
        for r in rows:
            if start is not None and r["date"] < start:
                continue
            if end is not None and r["date"] > end:
                continue
            out.append(r)
        return out

    # ── writes ────────────────────────────────────────────────────────────────
    def upsert(self, symbol: str, interval: str, records: List[Dict]) -> int:
        """Append records that aren't cached yet. Returns count of new bars added.

        Immutable: existing dates are never touched; only net-new dates are
        appended. The high-water mark for 'how much data exists' is the union,
        so a rerun with the same data is a no-op.
        """
        if not records:
            return 0
        path = self._csv_path(symbol, interval)

        # Load existing date set + path existence
        existing_dates = self.available_dates(symbol, interval)
        # Dedup within the incoming batch by date (last wins to normalize)
        by_date: Dict[str, Dict] = {}
        for r in records:
            d = r["date"]
            if d and (d not in by_date) and (d not in existing_dates):
                by_date[d] = {
                    "date": d,
                    "open": float(r["open"]),
                    "high": float(r["high"]),
                    "low": float(r["low"]),
                    "close": float(r["close"]),
                    "volume": int(float(r.get("volume", 0) or 0)),
                }

        if not by_date:
            return 0

        new_rows = sorted(by_date.values(), key=lambda r: r["date"])
        file_exists = path.exists()
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(
                f, fieldnames=["date", "open", "high", "low", "close", "volume"]
            )
            if not file_exists:
                writer.writeheader()
            for row in new_rows:
                writer.writerow(row)

        # Update the date index file.
        existing_dates.update(row["date"] for row in new_rows)
        try:
            self._index_path(symbol, interval).write_text(
                json.dumps(sorted(existing_dates))
            )
        except Exception as e:
            logger.warning("Could not write date index: %s", e)

        logger.info(
            "Cached %d new %s bars for %s (now %d total)",
            len(new_rows), interval, symbol, len(existing_dates),
        )
        return len(new_rows)

    # ── convenience ───────────────────────────────────────────────────────────
    def ensure_fetched(self, symbol: str, interval: str, fetcher,
                       min_bars: int = 0) -> List[Dict]:
        """Ensure we have >= min_bars bars; fetch+append from ``fetcher`` if not.

        ``fetcher`` is a zero-arg callable returning a list of {date,o,h,l,c,v}.
        Returns the full cached list for (symbol, interval).
        """
        existing = self.load(symbol, interval)
        if len(existing) >= min_bars:
            return existing
        fetched = fetcher()
        if fetched:
            self.upsert(symbol, interval, fetched)
        return self.load(symbol, interval)

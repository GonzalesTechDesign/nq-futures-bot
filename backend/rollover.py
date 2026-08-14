import logging
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger("ContractRollover")

class ContractRolloverManager:
    # CME NQ contract months: March (H), June (M), September (U), December (Z)
    EXPIRY_MONTHS = {3: "H", 6: "M", 9: "U", 12: "Z"}

    def __init__(self, threshold_days: int = 5):
        self.threshold_days = threshold_days

    def get_third_friday(self, year: int, month: int) -> date:
        # Find first day of month
        d = date(year, month, 1)
        # Find first Friday (weekday 4)
        days_to_friday = (4 - d.weekday() + 7) % 7
        first_friday = d.day + days_to_friday
        third_friday = first_friday + 14
        return date(year, month, third_friday)

    def get_front_contract(self, current_date: Optional[date] = None) -> tuple[str, int, date]:
        if current_date is None:
            current_date = datetime.utcnow().date()
        
        year = current_date.year
        month = current_date.month

        # Determine active quarterly cycle month >= current month
        cycle_months = [3, 6, 9, 12]
        target_month = 12
        target_year = year

        for m in cycle_months:
            expiry = self.get_third_friday(year, m)
            if current_date <= expiry:
                target_month = m
                target_year = year
                break
            if m == 12 and current_date > expiry:
                target_month = 3
                target_year = year + 1

        expiry_date = self.get_third_friday(target_year, target_month)
        days_left = (expiry_date - current_date).days
        code_letter = self.EXPIRY_MONTHS[target_month]
        year_digit = str(target_year)[-1]
        contract_symbol = f"NQ{code_letter}{year_digit}"

        return contract_symbol, days_left, expiry_date

    def check_rollover_status(self, current_date: Optional[date] = None) -> dict:
        symbol, days_left, expiry = self.get_front_contract(current_date)
        pending = days_left <= self.threshold_days
        if pending:
            logger.warning(f"ROLLOVER ALERT: Contract {symbol} expires in {days_left} days ({expiry}). Roll to next contract required.")
        return {
            "active_contract": symbol,
            "days_to_expiration": days_left,
            "expiry_date": str(expiry),
            "rollover_pending": pending
        }

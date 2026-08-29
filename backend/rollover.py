import logging
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger("ContractRollover")

class ContractRolloverManager:
    EXPIRY_MONTHS = {3: "H", 6: "M", 9: "U", 12: "Z"}

    def __init__(self, threshold_days: int = 5):
        self.threshold_days = threshold_days

    def get_third_friday(self, year: int, month: int) -> date:
        d = date(year, month, 1)
        days_to_friday = (4 - d.weekday() + 7) % 7
        first_friday = d.day + days_to_friday
        third_friday = first_friday + 14
        return date(year, month, third_friday)

    def get_front_contract(self, base_symbol: str = "NQ", current_date: Optional[date] = None) -> tuple[str, int, date]:
        if current_date is None:
            # Match the module's local day-rollover clock (risk manager uses
            # datetime.now().date()) rather than a UTC date.
            current_date = datetime.now().date()
        
        year = current_date.year
        month = current_date.month

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
        
        # Continuous contract notation e.g. NQ1! / MNQ1! or explicit quarterly code e.g. NQU6
        contract_symbol = f"{base_symbol}{code_letter}{year_digit}"

        return contract_symbol, days_left, expiry_date

    def check_rollover_status(self, base_symbol: str = "NQ", current_date: Optional[date] = None) -> dict:
        symbol, days_left, expiry = self.get_front_contract(base_symbol, current_date)
        pending = days_left <= self.threshold_days
        if pending:
            logger.warning(f"ROLLOVER ALERT: Contract {symbol} expires in {days_left} days ({expiry}). Roll required.")
        return {
            "base_symbol": base_symbol,
            "active_contract": symbol,
            "continuous_contract": f"{base_symbol}1!",
            "days_to_expiration": days_left,
            "expiry_date": str(expiry),
            "rollover_pending": pending
        }

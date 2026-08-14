import logging
import time
from functools import wraps

logger = logging.getLogger("CircuitBreaker")

class CircuitBreakerOpenException(Exception):
    pass

class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.state = "CLOSED" # CLOSED, OPEN, HALF-OPEN
        self.last_failure_time = 0.0

    def record_success(self):
        self.failure_count = 0
        self.state = "CLOSED"

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            logger.error(f"CIRCUIT BREAKER OPENED after {self.failure_count} failures.")

    def allow_request(self) -> bool:
        if self.state == "CLOSED":
            return True
        if self.state == "OPEN":
            if time.time() - self.last_failure_time >= self.recovery_timeout:
                self.state = "HALF-OPEN"
                logger.info("Circuit breaker entering HALF-OPEN state. Testing connection...")
                return True
            return False
        if self.state == "HALF-OPEN":
            return True
        return False

def with_circuit_breaker(breaker):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            b = breaker(args[0]) if callable(breaker) else breaker
            if not b.allow_request():
                raise CircuitBreakerOpenException("Circuit breaker is OPEN. Connection suspended temporarily.")
            try:
                res = func(*args, **kwargs)
                b.record_success()
                return res
            except Exception as e:
                b.record_failure()
                raise e
        return wrapper
    return decorator

"""
Tests for TradingView webhook endpoint and signal parsing.
"""

import os
import json
import pytest
from fastapi.testclient import TestClient

# Set a stable webhook secret before importing the app
os.environ["WEBHOOK_SECRET"] = "test-secret-abc123"

from backend.api import (
    app, WEBHOOK_SECRET, _parse_tv_payload, _verify_webhook_token,
    _webhook_rate_buckets, _redact_token, _sanitize_log_payload,
)
from backend.database import SessionLocal, DBTradingViewSignal, DBTrade
from backend.risk_manager import RiskManager
from backend.strategy import NQMomentumStrategy
from backend.ibkr_client import IBKRClient
from backend.rollover import ContractRolloverManager
from backend.auth import ADMIN_API_KEY

client = TestClient(app)
# backend.auth loads .env at import, so ADMIN_API_KEY is the current rotated key.
API_KEY = ADMIN_API_KEY
TV_WEBHOOK_URL = f"/api/v1/webhook/tradingview?token={WEBHOOK_SECRET}"
SIGNALS_URL = "/api/v1/webhook/signals"
TEST_URL = "/api/v1/webhook/test"


# ── Helper ──────────────────────────────────────────────────────────────────

def _reset_strategy():
    """Reset strategy position state between tests."""
    from backend.api import strategy, risk_mgr, paper_state
    strategy.position_qty = 0
    strategy.position_side = None
    strategy.entry_price = 0.0
    strategy.trailing_stop = 0.0
    strategy.bars_in_position = 0
    strategy.bars_since_last_trade = 999
    risk_mgr.killed = False
    risk_mgr.kill_reason = ""
    risk_mgr.total_pnl = 0.0
    risk_mgr.daily_pnl = 0.0
    risk_mgr.current_equity = risk_mgr.account_size
    risk_mgr.peak_equity = risk_mgr.account_size
    risk_mgr.trades_today = 0
    risk_mgr.consecutive_losses = 0
    risk_mgr.cooldown_remaining = 0
    paper_state.reset()
    # Clear webhook rate limiter buckets between tests
    _webhook_rate_buckets.clear()


# ── Payload Parsing Tests ───────────────────────────────────────────────────

class TestParseTVPayload:
    def test_json_payload(self):
        raw = json.dumps({"action": "BUY", "symbol": "NQ", "quantity": 2, "price": 18500, "strategy": "SMA", "alert_name": "Long Entry"})
        result = _parse_tv_payload(raw)
        assert result["action"] == "BUY"
        assert result["symbol"] == "NQ"
        assert result["quantity"] == 2
        assert result["price"] == 18500.0
        assert result["strategy"] == "SMA"
        assert result["alert_name"] == "Long Entry"

    def test_json_defaults(self):
        raw = json.dumps({"action": "SELL"})
        result = _parse_tv_payload(raw)
        assert result["action"] == "SELL"
        assert result["symbol"] == "NQ"
        assert result["quantity"] == 1
        assert result["price"] is None

    def test_json_with_token(self):
        raw = json.dumps({"action": "BUY", "token": "my-secret"})
        result = _parse_tv_payload(raw)
        assert result["token"] == "my-secret"

    def test_plain_text_full(self):
        result = _parse_tv_payload("BUY NQ 2 @18500")
        assert result["action"] == "BUY"
        assert result["symbol"] == "NQ"
        assert result["quantity"] == 2
        assert result["price"] == 18500.0

    def test_plain_text_no_qty(self):
        result = _parse_tv_payload("SELL MNQ")
        assert result["action"] == "SELL"
        assert result["symbol"] == "MNQ"
        assert result["quantity"] == 1
        assert result["price"] is None

    def test_plain_text_no_symbol(self):
        result = _parse_tv_payload("BUY 3 @18600")
        assert result["action"] == "BUY"
        assert result["symbol"] == "NQ"
        assert result["quantity"] == 3
        assert result["price"] == 18600.0

    def test_plain_text_flatten(self):
        result = _parse_tv_payload("FLATTEN NQ")
        assert result["action"] == "FLATTEN"
        assert result["symbol"] == "NQ"

    def test_plain_text_case_insensitive(self):
        result = _parse_tv_payload("buy nq 1 @18500")
        assert result["action"] == "BUY"
        assert result["symbol"] == "NQ"

    def test_invalid_payload(self):
        with pytest.raises(ValueError, match="Unable to parse"):
            _parse_tv_payload("this is garbage text")

    def test_json_list_ignored(self):
        with pytest.raises(ValueError, match="Unable to parse"):
            _parse_tv_payload('["BUY", "SELL"]')


# ── Token Verification Tests ────────────────────────────────────────────────

class TestVerifyWebhookToken:
    def test_valid_query_token(self):
        assert _verify_webhook_token(token="test-secret-abc123") is True

    def test_valid_body_token(self):
        assert _verify_webhook_token(body_token="test-secret-abc123") is True

    def test_invalid_token(self):
        assert _verify_webhook_token(token="wrong") is False

    def test_no_token(self):
        assert _verify_webhook_token() is False

    def test_empty_token(self):
        assert _verify_webhook_token(token="") is False


# ── Webhook Integration Tests ──────────────────────────────────────────────

class TestWebhookEndpoint:
    def setup_method(self):
        _reset_strategy()

    def test_buy_json_executed(self):
        payload = {"action": "BUY", "symbol": "NQ", "quantity": 1, "price": 18500, "alert_name": "Test Buy"}
        resp = client.post(TV_WEBHOOK_URL, json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "EXECUTED"
        assert data["action"] == "BUY"
        assert data["quantity"] == 1
        assert data["price"] == 18500.0
        assert "signal_id" in data
        assert "trade_id" in data
        assert "stop_loss" in data
        assert "take_profit" in data

    def test_sell_json_executed(self):
        payload = {"action": "SELL", "symbol": "NQ", "quantity": 1, "price": 18600}
        resp = client.post(TV_WEBHOOK_URL, json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "EXECUTED"
        assert data["action"] == "SELL"

    def test_buy_plain_text(self):
        resp = client.post(TV_WEBHOOK_URL, content="BUY NQ 1 @18500", headers={"Content-Type": "text/plain"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "EXECUTED"

    def test_flatten_with_position(self):
        from backend.api import strategy
        strategy.position_qty = 2
        strategy.position_side = "LONG"
        strategy.entry_price = 18500.0

        payload = {"action": "FLATTEN", "symbol": "NQ"}
        resp = client.post(TV_WEBHOOK_URL, json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "EXECUTED"
        assert data["action"] == "FLATTEN"
        assert data["quantity"] == 2
        assert data["exit_side"] == "SELL"

    def test_flatten_already_flat(self):
        payload = {"action": "FLATTEN", "symbol": "NQ"}
        resp = client.post(TV_WEBHOOK_URL, json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "EXECUTED"
        assert data["message"] == "Already flat."

    def test_auth_missing_token(self):
        payload = {"action": "BUY", "symbol": "NQ", "quantity": 1}
        resp = client.post("/api/v1/webhook/tradingview", json=payload)
        assert resp.status_code == 401

    def test_auth_wrong_token(self):
        payload = {"action": "BUY", "symbol": "NQ", "quantity": 1}
        resp = client.post("/api/v1/webhook/tradingview?token=wrong-token", json=payload)
        assert resp.status_code == 401

    def test_auth_body_token(self):
        payload = {"action": "BUY", "symbol": "NQ", "quantity": 1, "price": 18500, "token": WEBHOOK_SECRET}
        resp = client.post("/api/v1/webhook/tradingview", json=payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "EXECUTED"

    def test_invalid_action(self):
        payload = {"action": "HOLD", "symbol": "NQ", "quantity": 1}
        resp = client.post(TV_WEBHOOK_URL, json=payload)
        assert resp.status_code == 400

    def test_invalid_quantity_zero(self):
        payload = {"action": "BUY", "symbol": "NQ", "quantity": 0}
        resp = client.post(TV_WEBHOOK_URL, json=payload)
        assert resp.status_code == 400

    def test_invalid_quantity_negative(self):
        payload = {"action": "BUY", "symbol": "NQ", "quantity": -1}
        resp = client.post(TV_WEBHOOK_URL, json=payload)
        assert resp.status_code == 400

    def test_unparseable_payload_logged(self):
        resp = client.post(TV_WEBHOOK_URL, content="totally invalid garbage", headers={"Content-Type": "text/plain"})
        assert resp.status_code == 400

    def test_risk_rejection_logged(self):
        """Risk rejection should still log the signal as REJECTED."""
        from backend.api import risk_mgr
        risk_mgr.killed = True
        risk_mgr.kill_reason = "CUMULATIVE_DLL"
        try:
            payload = {"action": "BUY", "symbol": "NQ", "quantity": 1, "price": 18500}
            resp = client.post(TV_WEBHOOK_URL, json=payload)
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "REJECTED"
            assert "DLL" in data["reject_reason"] or "locked" in data["reject_reason"].lower()
            assert "signal_id" in data
        finally:
            risk_mgr.killed = False
            risk_mgr.kill_reason = ""


# ── Signals List Endpoint Tests ─────────────────────────────────────────────

class TestSignalsEndpoint:
    def test_requires_api_key(self):
        resp = client.get(SIGNALS_URL)
        assert resp.status_code == 422  # missing required header

    def test_wrong_api_key(self):
        resp = client.get(SIGNALS_URL, headers={"X-API-Key": "wrong-key"})
        assert resp.status_code == 401

    def test_returns_signals(self):
        resp = client.get(SIGNALS_URL, headers={"X-API-Key": API_KEY})
        assert resp.status_code == 200
        data = resp.json()
        assert "signals" in data
        assert "count" in data
        assert isinstance(data["signals"], list)

    def test_limited_to_50(self):
        resp = client.get(SIGNALS_URL, headers={"X-API-Key": API_KEY})
        data = resp.json()
        assert data["count"] <= 50


# ── Test Endpoint Tests ────────────────────────────────────────────────────

class TestTestEndpoint:
    def setup_method(self):
        _reset_strategy()

    def test_requires_api_key(self):
        resp = client.post(TEST_URL, json={"action": "BUY"})
        assert resp.status_code == 422  # missing required header

    def test_buy_executed(self):
        resp = client.post(TEST_URL, json={
            "action": "BUY", "symbol": "NQ", "quantity": 1, "price": 18500,
            "alert_name": "Test Buy",
        }, headers={"X-API-Key": API_KEY})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "EXECUTED"
        assert data["parsed_action"] == "BUY"

    def test_risk_rejection(self):
        from backend.api import risk_mgr
        risk_mgr.killed = True
        risk_mgr.kill_reason = "TEST"
        try:
            resp = client.post(TEST_URL, json={"action": "BUY", "quantity": 1}, headers={"X-API-Key": API_KEY})
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "REJECTED"
        finally:
            risk_mgr.killed = False
            risk_mgr.kill_reason = ""

    def test_flatten(self):
        from backend.api import strategy
        strategy.position_qty = 3
        strategy.position_side = "LONG"
        strategy.entry_price = 18500.0

        resp = client.post(TEST_URL, json={"action": "FLATTEN", "price": 18550}, headers={"X-API-Key": API_KEY})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "EXECUTED"
        assert data["parsed_qty"] == 3

    def test_invalid_action(self):
        resp = client.post(TEST_URL, json={"action": "HOLD"}, headers={"X-API-Key": API_KEY})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "BLOCKED"


# ── Signal Logging Verification ─────────────────────────────────────────────

class TestSignalLogging:
    """Verify that signals are persisted to the tv_signals table."""

    def setup_method(self):
        _reset_strategy()

    def test_executed_signal_stored(self):
        payload = {"action": "BUY", "symbol": "NQ", "quantity": 1, "price": 18500, "strategy": "SMA", "alert_name": "Test"}
        resp = client.post(TV_WEBHOOK_URL, json=payload)
        assert resp.status_code == 200
        sig_id = resp.json()["signal_id"]

        db = SessionLocal()
        try:
            sig = db.query(DBTradingViewSignal).filter(DBTradingViewSignal.id == sig_id).first()
            assert sig is not None
            assert sig.parsed_action == "BUY"
            assert sig.parsed_symbol == "NQ"
            assert sig.parsed_qty == 1
            assert sig.status == "EXECUTED"
            assert sig.execution_price == 18500.0
            assert sig.raw_payload is not None
            assert sig.strategy_name == "SMA"
            assert sig.alert_name == "Test"
        finally:
            db.close()

    def test_rejected_signal_stored(self):
        from backend.api import risk_mgr
        risk_mgr.killed = True
        risk_mgr.kill_reason = "TEST"
        try:
            payload = {"action": "BUY", "symbol": "NQ", "quantity": 1, "price": 18500}
            resp = client.post(TV_WEBHOOK_URL, json=payload)
            assert resp.status_code == 200
            sig_id = resp.json()["signal_id"]

            db = SessionLocal()
            try:
                sig = db.query(DBTradingViewSignal).filter(DBTradingViewSignal.id == sig_id).first()
                assert sig is not None
                assert sig.status == "REJECTED"
                assert sig.reject_reason is not None
            finally:
                db.close()
        finally:
            risk_mgr.killed = False
            risk_mgr.kill_reason = ""

    def test_blocked_auth_signal_stored(self):
        payload = {"action": "BUY", "symbol": "NQ", "quantity": 1}
        resp = client.post("/api/v1/webhook/tradingview?token=wrong", json=payload)
        assert resp.status_code == 401

        db = SessionLocal()
        try:
            sig = db.query(DBTradingViewSignal).order_by(DBTradingViewSignal.id.desc()).first()
            assert sig is not None
            assert sig.status == "BLOCKED"
            assert "token" in (sig.reject_reason or "").lower()
        finally:
            db.close()

    def test_trade_recorded_on_execution(self):
        payload = {"action": "BUY", "symbol": "NQ", "quantity": 1, "price": 18500, "alert_name": "Exec Test"}
        resp = client.post(TV_WEBHOOK_URL, json=payload)
        assert resp.status_code == 200
        trade_id = resp.json()["trade_id"]

        db = SessionLocal()
        try:
            trade = db.query(DBTrade).filter(DBTrade.trade_id == trade_id).first()
            assert trade is not None
            assert trade.symbol == "NQ"
            assert trade.side == "BUY"
            assert trade.quantity == 1
            assert trade.price == 18500.0
            assert trade.order_type == "MARKET"
        finally:
            db.close()


# ── Security Tests ────────────────────────────────────────────────────────

class TestWebhookSecurity:
    """Security-focused tests for the webhook endpoint."""

    def setup_method(self):
        _reset_strategy()

    def test_payload_too_large_rejected(self):
        """Payloads > 8KB should be rejected with 413."""
        oversized = "x" * 10000
        resp = client.post(TV_WEBHOOK_URL, content=oversized, headers={"Content-Type": "text/plain"})
        assert resp.status_code == 413
        data = resp.json()
        assert data["status"] == "error"
        assert "too large" in data["message"].lower()

    def test_payload_exactly_at_limit_accepted(self):
        """Payloads at exactly 8KB should be accepted (if valid)."""
        # 8192 bytes of valid plain-text signal — may fail parsing but shouldn't be 413
        payload = "BUY NQ 1 @18500" + " " * (8192 - 15)
        resp = client.post(TV_WEBHOOK_URL, content=payload, headers={"Content-Type": "text/plain"})
        # Should NOT be 413 (payload size is within limit)
        assert resp.status_code != 413

    def test_rate_limit_enforced(self):
        """Sending > 10 requests within 60s from same IP should get 429."""
        payload = {"action": "BUY", "symbol": "NQ", "quantity": 1, "price": 18500}
        # First 10 should succeed (or at least not be 429)
        for i in range(10):
            resp = client.post(TV_WEBHOOK_URL, json=payload)
            assert resp.status_code != 429, f"Request {i+1} should not be rate-limited"

        # 11th should be rate-limited
        resp = client.post(TV_WEBHOOK_URL, json=payload)
        assert resp.status_code == 429
        data = resp.json()
        assert data["status"] == "error"
        assert "rate limit" in data["message"].lower()

    def test_rate_limit_resets_after_window(self):
        """Rate limiter bucket should clear old entries (tested by _reset_strategy)."""
        # After setup_method clears buckets, first request should succeed
        payload = {"action": "BUY", "symbol": "NQ", "quantity": 1, "price": 18500}
        resp = client.post(TV_WEBHOOK_URL, json=payload)
        assert resp.status_code == 200

    def test_token_never_logged(self, caplog):
        """The webhook token/secret must never appear in log output."""
        import logging
        with caplog.at_level(logging.INFO, logger="APIServer"):
            payload = {"action": "BUY", "symbol": "NQ", "quantity": 1, "price": 18500}
            resp = client.post(TV_WEBHOOK_URL, json=payload)
            assert resp.status_code == 200

        # The webhook secret must NOT appear in any log message
        for record in caplog.records:
            assert WEBHOOK_SECRET not in record.message, (
                f"Webhook token leaked into log: {record.message}"
            )

    def test_auth_failure_token_not_logged(self, caplog):
        """Even on auth failure, the token must not appear in logs."""
        import logging
        with caplog.at_level(logging.INFO, logger="APIServer"):
            payload = {"action": "BUY", "symbol": "NQ", "quantity": 1}
            resp = client.post("/api/v1/webhook/tradingview?token=super-secret-value", json=payload)
            assert resp.status_code == 401

        for record in caplog.records:
            assert "super-secret-value" not in record.message, (
                f"Rejected token leaked into log: {record.message}"
            )

    def test_body_token_never_leaks_to_logs(self, caplog):
        """A body-supplied token must not leak into log output.

        The body-token auth path is the worst case: the raw body (which contains
        the secret verbatim) is logged, so it must be redacted before logging.
        """
        import logging
        secret = "test-secret-abc123"
        with caplog.at_level(logging.INFO, logger="APIServer"):
            payload = {
                "action": "BUY", "symbol": "NQ", "quantity": 1,
                "price": 18500, "token": secret,
            }
            resp = client.post("/api/v1/webhook/tradingview", json=payload)
            assert resp.status_code == 200
            assert resp.json()["status"] == "EXECUTED"

        for record in caplog.records:
            text = record.getMessage()
            assert secret not in text, (
                f"Body token leaked into log: {text}"
            )
            assert '"token"' not in text or "REDACTED" in text or "token" not in text.lower()

    def test_body_token_sensitive_secret_not_in_caplog(self, caplog):
        """R1(a) regression: a body-supplied token must never reach logs.

        POST a JSON body that contains `"token":"sensitive-secret-123"` and
        assert the value never appears anywhere in captured log output.
        Uses a wrong secret on purpose: the sanitize-then-log step happens
        before auth, so a scrubbed log line is the only way the secret is not
        exposed even when authentication will fail.
        """
        import logging
        secret = "sensitive-secret-123"
        with caplog.at_level(logging.INFO, logger="APIServer"):
            payload = {
                "action": "BUY", "symbol": "NQ", "quantity": 1,
                "price": 18500, "token": secret,
            }
            resp = client.post("/api/v1/webhook/tradingview", json=payload)
            # Not the real webhook secret, so auth must fail — but the log
            # line for the received request is still emitted beforehand.
            assert resp.status_code == 401

        assert secret not in caplog.text, (
            "Body token leaked into captured logs"
        )
        for record in caplog.records:
            assert secret not in record.getMessage(), (
                f"Body token leaked into log: {record.getMessage()}"
            )

    def test_nested_token_redacted_by_both_sanitizers(self):
        """R3: nested 'token' keys must be scrubbed, not just top-level ones.

        Both `_redact_token` (persisted payloads) and `_sanitize_log_payload`
        (log lines) must recurse into nested dicts/lists so a secret buried at
        any depth never survives to storage or logs.
        """
        raw = json.dumps({
            "payload": {
                "token": "sensitive-nested-1",
                "depth": [{"token": "sensitive-nested-2"}],
            }
        })
        for result in (_redact_token(raw), _sanitize_log_payload(raw)):
            assert "sensitive-nested-1" not in result, (
                f"nested token leaked from redaction: {result}"
            )
            assert "sensitive-nested-2" not in result, (
                f"deeper nested token leaked from redaction: {result}"
            )

    def test_nested_token_never_logged(self, caplog):
        """R3 regression: a nested body token must never reach captured logs.

        POST a JSON body with a secret nested under 'payload' (and deeper,
        inside a list) — the same shape that previously survived top-level-only
        redaction and was written to the uvicorn log line and persisted into
        tv_signals.raw_payload. Assert neither secret appears in any log line.
        """
        import logging
        nested_1 = "sensitive-nested-1"
        nested_2 = "sensitive-nested-2"
        with caplog.at_level(logging.INFO, logger="APIServer"):
            payload = {
                "action": "BUY", "symbol": "NQ", "quantity": 1, "price": 18500,
                "payload": {
                    "token": nested_1,
                    "depth": [{"token": nested_2}],
                },
            }
            # Authenticate via the query param (the nested body token is not
            # used for auth) so the happy path — including persistence of the
            # redacted raw_payload via _redact_token — is exercised.
            resp = client.post(TV_WEBHOOK_URL, json=payload)
            assert resp.status_code == 200
            assert resp.json()["status"] == "EXECUTED"

        assert nested_1 not in caplog.text, (
            "Nested body token leaked into captured logs"
        )
        assert nested_2 not in caplog.text, (
            "Deeply nested body token leaked into captured logs"
        )
        for record in caplog.records:
            assert nested_1 not in record.getMessage(), (
                f"Nested body token leaked into log: {record.getMessage()}"
            )
            assert nested_2 not in record.getMessage(), (
                f"Deeply nested body token leaked into log: {record.getMessage()}"
            )

    def test_uvicorn_access_filter_redacts(self):
        """The uvicorn access-logger filter redacts ?token=... query strings."""
        from run_server import TokenRedactFilter

        filt = TokenRedactFilter()
        path = "/api/v1/webhook/tradingview?token=test-secret-abc123&action=BUY"
        redacted = filt._redact(path)
        assert "test-secret-abc123" not in redacted
        assert "token=***REDACTED***" in redacted
        assert redacted == "/api/v1/webhook/tradingview?token=***REDACTED***&action=BUY"

        # Also verify via an actual logging record with args (how uvicorn logs).
        import logging
        record = logging.LogRecord(
            name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
            msg="%s - \"%s %s HTTP/1.1\" %d",
            args=("127.0.0.1:5555", "GET", "/api/v1/webhook/tradingview?token=test-secret-abc123", 200),
            exc_info=None,
        )
        assert filt.filter(record)
        formatted = record.getMessage()
        assert "test-secret-abc123" not in formatted
        assert "token=***REDACTED***" in formatted

    def test_uvicorn_access_filter_redacts_preformatted_line(self):
        """The filter must also scrub a fully-formatted access line (no args).

        Some uvicorn/formatter wiring emits the already-formatted request line
        as the record message itself, so the filter must redact `msg` as well.
        """
        from run_server import TokenRedactFilter

        filt = TokenRedactFilter()
        line = ('127.0.0.1:5555 - "GET /api/v1/webhook/tradingview'
                '?token=sensitive-secret-123 HTTP/1.1" 200')
        import logging
        record = logging.LogRecord(
            name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
            msg=line, args=None, exc_info=None,
        )
        assert filt.filter(record)
        formatted = record.getMessage()
        assert "sensitive-secret-123" not in formatted
        assert "token=***REDACTED***" in formatted

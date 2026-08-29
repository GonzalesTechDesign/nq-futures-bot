#!/usr/bin/env python3
"""
Run the NQ Futures Bot API server.

Usage:
    source venv/bin/activate
    python run_server.py              # Port 8888 (default)
    sudo python run_server.py --port 80   # Port 80 for TradingView webhooks

Dashboard: open frontend/index.html in browser
API docs:  http://localhost:8888/docs (or port 80)
"""

import uvicorn
import sys
import os
import re
import logging
import argparse

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


class TokenRedactFilter(logging.Filter):
    """Redact auth tokens from uvicorn access log lines.

    The webhook is authenticated via `?token=<secret>` in the query string,
    and uvicorn's access logger writes the full request path (including the
    query string) verbatim. Replace any `token=...` fragment with a redacted
    placeholder so the secret never lands in server.log or console output.

    uvicorn emits the access line as a %-style format string whose positional
    args include the request path (msg='%s - "%s %s HTTP/%s" %d',
    args=(addr, method, "/path?token=...", ...)), so the path is redacted in
    `record.args`. Redacting `record.msg` as well covers any logger wiring
    that formats the line *before* the record is created.
    """

    _TOKEN_PATTERN = re.compile(r"(token=)([^&\s\"]+)", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = self._redact(record.msg)
            if record.args:
                record.args = tuple(
                    self._redact(a) if isinstance(a, str) else a for a in record.args
                )
        except Exception:  # never let redaction break logging
            pass
        return True

    def _redact(self, text: str) -> str:
        return self._TOKEN_PATTERN.sub(r"\1***REDACTED***", text)

def main():
    parser = argparse.ArgumentParser(description="NQ Futures Bot API Server")
    parser.add_argument("--port", "-p", type=int, default=int(os.getenv("API_PORT", "8888")), help="Port to run on (default: 8888, use 80 for TradingView)")
    parser.add_argument("--host", type=str, default=os.getenv("API_HOST", "0.0.0.0"), help="Host to bind to")
    parser.add_argument("--no-reload", action="store_true", help="Disable auto-reload")
    args = parser.parse_args()

    # Check for port 80 without root
    if args.port < 1024 and os.geteuid() != 0:
        print(f"\n⚠️  Port {args.port} requires root privileges.")
        print(f"   Run with: sudo python run_server.py --port {args.port}")
        print(f"   Or use default port 8888: python run_server.py")
        sys.exit(1)

    PORT = args.port
    HOST = args.host
    RELOAD = not args.no_reload

    print(f"\n  NQ Futures Bot — Lucid Trading $50K Eval")
    print(f"  API:       http://{HOST}:{PORT}/api/v1")
    print(f"  API Docs:  http://{HOST}:{PORT}/docs")
    print(f"  Dashboard: frontend/index.html")
    print(f"  Port:      {PORT}")
    if PORT == 80:
        print(f"  Webhook:   http://YOUR_IP/api/v1/webhook/tradingview?token=...")
    else:
        print(f"  Webhook:   http://YOUR_IP:{PORT}/api/v1/webhook/tradingview?token=...")
    print()

    # Redact auth tokens from uvicorn access logs before the server starts.
    # uvicorn writes the request path (incl. `?token=<secret>`) verbatim, so a
    # bare access logger would leak the webhook secret to server.log.
    redact_filter = TokenRedactFilter()
    logging.getLogger("uvicorn.access").addFilter(redact_filter)

    uvicorn.run("backend.api:app", host=HOST, port=PORT, reload=RELOAD)

if __name__ == "__main__":
    main()

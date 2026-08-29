"""
API Authentication for NQ Futures Bot.

Admin API key loaded from .env file or ADMIN_API_KEY environment variable.
Generates a random key on first run if neither is set.
"""

import os
import secrets
import logging
from pathlib import Path
from dotenv import load_dotenv
from fastapi import Header, HTTPException, status

logger = logging.getLogger("Auth")

# Load .env file
_env_path = Path("/home/miggs101/Development/nq-futures-bot/.env")
load_dotenv(_env_path)


def _get_or_create_api_key() -> str:
    """Get API key from env, or generate one and save to .env."""
    key = os.getenv("ADMIN_API_KEY")
    if key:
        return key

    # Generate a new random key
    key = secrets.token_urlsafe(32)
    logger.warning("No ADMIN_API_KEY env var set — generated new random key.")

    # Save to .env file for next run
    if _env_path.exists():
        existing = _env_path.read_text()
        if "ADMIN_API_KEY" not in existing:
            _env_path.write_text(existing + f"ADMIN_API_KEY={key}\n")
    else:
        _env_path.write_text(f"ADMIN_API_KEY={key}\n")

    os.environ["ADMIN_API_KEY"] = key
    logger.warning(f"Generated API key saved to {_env_path}.")
    return key


ADMIN_API_KEY = _get_or_create_api_key()


def verify_api_key(x_api_key: str = Header(...)):
    if not secrets.compare_digest(x_api_key, ADMIN_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key for control operations."
        )
    return x_api_key

"""
Controlled-restart deploy mechanism (option A).

The self-improving agent harness never mutates trading-engine code. When a
parameter change is approved by a human, this module applies it the safe way:

  1. Validate: params are in-bounds and the resulting YAML is well-formed.
  2. Write: update ONLY the ``strategy:`` block of config/risk_config.yaml,
     preserving all risk_limits and comments above it.
  3. Restart: restart the running bot via its systemd unit, then wait for the
     API to come back healthy.
  4. Audit: append a versioned, immutable entry to a deploy log.

This is the ONLY sanctioned path for changing trading behavior. It is
deterministic code — not something an LLM can call push-button without a human
approval gate in front of it.
"""

import csv
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from backend.strategy_evaluator import PARAM_BOUNDS

logger = logging.getLogger("Deploy")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RISK_CONFIG = PROJECT_ROOT / "config" / "risk_config.yaml"
DEFAULT_DEPLOY_LOG = PROJECT_ROOT / "data" / "deployments.csv"
SERVICE_NAME = os.getenv("NQ_SERVICE_NAME", "nq-futures-bot")
STATUS_URL = os.getenv("NQ_STATUS_URL", "http://127.0.0.1:80/api/v1/status")
RESTART_TIMEOUT = int(os.getenv("NQ_RESTART_TIMEOUT", "90"))  # seconds


def _risk_config_path() -> Path:
    return Path(os.getenv("NQ_RISK_CONFIG", DEFAULT_RISK_CONFIG))


def _deploy_log_path() -> Path:
    return Path(os.getenv("NQ_DEPLOY_LOG", DEFAULT_DEPLOY_LOG))

# strategy: block header comment (shown when we first add the block)
_STRATEGY_HEADER = (
    "# Strategy parameters \u2014 shared by the LIVE NQMomentumStrategy AND the\n"
    "# BacktestEngine (single source of truth). This is the tunable surface the\n"
    "# self-improving agent's search will float over. NOTE: changing these requires\n"
    "# a server restart to take effect on the live bot (config is read at\n"
    "# construction time).\n"
)


class DeployError(RuntimeError):
    pass


def _validate(params: Dict[str, Any]) -> List[str]:
    """Return a list of validation errors (empty == valid)."""
    errors = []
    for key, (lo, hi) in PARAM_BOUNDS.items():
        if key not in params:
            errors.append(f"missing required parameter: {key}")
            continue
        val = params[key]
        if not (lo <= val <= hi):
            errors.append(f"{key}={val} out of bounds [{lo}, {hi}]")
    if "sma_fast" in params and "sma_slow" in params:
        if params["sma_fast"] >= params["sma_slow"]:
            errors.append("sma_fast must be < sma_slow")
    return errors


def _read_strategy_block() -> Dict[str, Any]:
    with open(_risk_config_path(), "r") as f:
        cfg = yaml.safe_load(f) or {}
    return dict(cfg.get("strategy", {}) or {})


def _render_strategy_block(params: Dict[str, Any]) -> str:
    """Render the strategy block as YAML text (two-space indent, deterministic)."""
    body = yaml.safe_dump(
        params, default_flow_style=False, sort_keys=True, allow_unicode=True
    )
    # Re-indent under the top-level "strategy:" key.
    lines = ["strategy:"]
    for ln in body.rstrip("\n").split("\n"):
        lines.append("  " + ln)
    return "\n".join(lines) + "\n"


def _write_strategy_block(params: Dict[str, Any]) -> Path:
    """Replace the strategy block in place, preserving everything above it."""
    risk_config = _risk_config_path()
    text = risk_config.read_text()
    # Locate the line where the top-level "strategy:" key starts.
    lines = text.split("\n")
    idx = None
    for i, ln in enumerate(lines):
        if ln.rstrip() == "strategy:" or ln.startswith("strategy:"):
            idx = i
            break
    head = "\n".join(lines[:idx]).rstrip("\n") if idx is not None else text.rstrip("\n")

    block = "\n\n" + _STRATEGY_HEADER + _render_strategy_block(params).rstrip("\n") + "\n"
    new_text = head + block

    # Safety: ensure the write is still a valid YAML with a strategy block.
    try:
        parsed = yaml.safe_load(new_text)
    except yaml.YAMLError as e:
        raise DeployError(f"Refusing to write invalid YAML: {e}")
    if not isinstance(parsed.get("strategy"), dict):
        raise DeployError("Refusing to write a config without a strategy block")

    # Atomic-ish write (write temp, then replace) to avoid a torn file.
    tmp = risk_config.with_suffix(".yaml.tmp")
    tmp.write_text(new_text)
    os.replace(tmp, risk_config)
    return risk_config


def _restart_service() -> None:
    """Restart the bot's systemd service and wait for it to come back healthy."""
    try:
        subprocess.run(
            ["systemctl", "restart", SERVICE_NAME],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        raise DeployError("systemctl not found (are we on a non-systemd host?)")
    except subprocess.CalledProcessError as e:
        raise DeployError(f"systemctl restart failed: {e.stderr.strip()}")

    # Wait for the API to become healthy again.
    deadline = time.time() + RESTART_TIMEOUT
    while time.time() < deadline:
        if _status_ok():
            return
        time.sleep(2)
    raise DeployError(
        f"Service restarted but /api/v1/status not healthy within "
        f"{RESTART_TIMEOUT}s"
    )


def _status_ok() -> bool:
    """Return True if the status endpoint responds with a RUNNING/PAPER state."""
    import urllib.request

    try:
        with urllib.request.urlopen(STATUS_URL, timeout=5) as r:
            body = json.loads(r.read().decode())
            return body.get("status") in ("RUNNING", "running", "running_paper")
    except Exception:
        return False


def _log_deploy(params: Dict[str, Any], approver: str, note: str) -> None:
    """Append an immutable deploy record."""
    log_path = _deploy_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    new = not log_path.exists()
    with open(log_path, "a", newline="") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(["ts", "approver", "note", "params_json"])
        writer.writerow([
            datetime.now(timezone.utc).isoformat(),
            approver,
            note,
            json.dumps(params, sort_keys=True),
        ])


def deploy_strategy(params: Dict[str, Any], approver: str = "auto",
                    note: str = "", restart: bool = True) -> Dict[str, Any]:
    """
    Apply an approved parameter set to the live bot via controlled restart.

    Returns a result dict. Raises DeployError on any failure (nothing is
    partially applied: the YAML write is validated before restart, and a
    restart that doesn't come back healthy raises before we report success).
    """
    params = dict(params)  # defensive copy
    errors = _validate(params)
    if errors:
        raise DeployError("invalid params: " + "; ".join(errors))

    before = _read_strategy_block()
    _write_strategy_block(params)

    if restart:
        _restart_service()

    # Confirm the live service actually picked up the new params.
    now = _read_strategy_block()
    if restart:
        logger.info("Deployed new strategy params (restart). approver=%s", approver)

    _log_deploy(params, approver, note)
    return {
        "applied": True,
        "params": params,
        "before": before,
        "after": now,
        "restarted": restart,
        "approver": approver,
    }

# NQ Futures Trading Bot — Paper & Live Trading Startup Guide

To transition our NQ Futures Trading Bot from simulation/development to **actively trading in paper mode** (and eventually live mode), follow this step-by-step checklist.

---

## Step 1: Configure Environment Variables
1. Copy the secure environment template:
   ```bash
   cd /home/miggs101/Development/nq-futures-bot
   cp config/.env.example .env
   ```
2. Edit `.env` and insert your actual **Databento API key**:
   ```env
   TRADING_MODE=PAPER
   IBKR_HOST=127.0.0.1
   IBKR_PORT=7497
   IBKR_CLIENT_ID=1
   DATABENTO_API_KEY=db-your-actual-databento-api-key
   ```

---

## Step 2: Start Interactive Brokers TWS or IB Gateway (Paper Trading)
NautilusTrader communicates locally with Interactive Brokers via the TWS API.
1. Launch **Interactive Brokers TWS** or **IB Gateway** on your machine.
2. Log in using your **Paper Trading account** credentials.
3. Verify TWS API Settings (File → Global Configuration → API → Settings):
   - **Enable ActiveX and Socket Clients:** Checked ✅
   - **Socket Port:** `7497` (Paper trading default) ✅
   - **Bypass Order Messaging for API Orders:** Checked (optional, avoids popups) ✅
   - **Trusted IPs:** Include `127.0.0.1` ✅

---

## Step 3: Connect NautilusTrader Live/Paper Engine
To connect NautilusTrader's core execution engine to IBKR and Databento instead of mock feeds, instantiate NautilusTrader's `TradingNode` in `backend/engine.py`:
- **Data Client:** `DatabentoLiveDataClient` configured with your `DATABENTO_API_KEY` and CME GLBX.MDP3 NQ subscription (`NQU6`).
- **Execution Client:** `InteractiveBrokersExecutionClient` connected to `127.0.0.1:7497`.
- **Strategy Deployment:** Register `NQMomentumStrategy` with the NautilusTrader `TradingNode`.

---

## Step 4: Run Paper Trading & Verify Execution
1. Start the API server and dashboard:
   ```bash
   cd /home/miggs101/Development/nq-futures-bot
   PYTHONPATH=. ./venv/bin/uvicorn backend.api:app --host 0.0.0.0 --port 8000 --reload
   ```
2. Open `frontend/index.html` in your browser.
3. Click **"Start Bot"** on the dashboard. 
4. Verify on the persistent banner that **PAPER TRADING MODE ACTIVE (IBKR PORT 7497)** is displayed, and check the trade log and IBKR TWS screen to confirm orders are being placed in your paper account.

---

## 🚨 Step 5: Transitioning to Live Trading (Restricted)
Per our non-negotiable safety constraints, **live trading (real capital) cannot be enabled casually**:
1. **Paper Validation:** The bot must successfully run in paper trading mode through at least one complete NQ quarterly contract rollover and market session without risk breaches.
2. **Security-Agent Audit:** `@security` must review API endpoints, credential storage, and TWS local bindings.
3. **Review-Agent Sign-Off:** `@reviewer` must verify backtest/live parity and walk-forward validation.
4. **Configuration Override:** Once authorized, update `config/risk_config.yaml`:
   ```yaml
   risk_limits:
     allow_live_trading: true
   ```
   and switch the environment variable `TRADING_MODE=LIVE` (IBKR port `7496`).

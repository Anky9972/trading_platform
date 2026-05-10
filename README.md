# Personal Trading Execution Platform

## Overview
A highly reliable, automated Python trading execution platform designed for algorithmic trading. The platform accepts a user-uploaded strategy file executed as an isolated subprocess and executes equity trades on NSE via Angel One SmartAPI. 

It is built on a fail-closed architecture that strictly separates strategy logic from order execution and risk validation. This architecture guarantees that a hung or buggy strategy cannot bypass risk checks, wipe out an account, or cause runaway loops. Safety is enforced structurally through multi-process isolation, rigorous watchdog monitoring, and broker-reconciled position states.

## Architecture
The system utilizes a multi-process architecture communicating via Unix domain sockets (Linux/macOS) for security and speed. The Trading Engine and Risk Server operate independently to enforce strict pre-trade invariants.

```text
       CLI (trade.py) --HMAC--> Trading Engine (engine.py)
                                     |
                                     v
Strategy Subprocess --> [Signal] --> Risk Server (Unix Socket)
                                     | (If Approved)
                                     v
                                 Order Manager
                                     |
                                     v
                               Angel One Broker
```

**Auxiliary Monitoring Services:**
*   **Watchdog (`monitor/watchdog.py`):** Monitors system heartbeat, calculates continuous PnL, enforces stop losses, and manages emergency liquidation.
*   **Price Validator (`store/price_validator.py`):** Secondary check against Yahoo Finance to prevent bad broker ticks from triggering false stops.
*   **Emergency Executor (`monitor/emergency_executor.py`):** Hardcoded fail-safe to liquidate positions automatically if the engine hangs.

## Repository Structure

```text
trading_platform/
 ├── config/
 │    └── settings.py              # Central configuration via frozen environment variable dataclasses
 ├── store/
 │    ├── database.py              # SQLite WAL schema handling the full order lifecycle
 │    ├── event_log.py             # Append-only JSONL event trail for auditory tracing
 │    ├── price_cache.py           # Shared memory SQLite table bridging Watchdog and Risk Server
 │    └── price_validator.py       # Secondary yfinance price pipeline
 ├── core/
 │    ├── models.py                # Core domain schemas (Signal, Order, Position)
 │    ├── order_manager.py         # Order state machine processing CREATED to FILLED/UNKNOWN
 │    ├── reconciliation.py        # Logic to overwrite local position drift by scanning broker book
 │    └── session_store.py         # Fernet encrypted broker token store
 ├── broker/
 │    ├── base.py                  # Abstract exchange integration interface
 │    ├── angel.py                 # Angel One Smart API wrapper with circuit breaking logic
 │    └── paper.py                 # Deterministic execution for tests
 ├── risk/
 │    ├── client.py                # Unix socket Risk Engine client
 │    ├── server.py                # Standalone daemon processing 12 pre-trade invariants
 │    └── kill_switch.py           # Global trading halt enforcement logic
 ├── monitor/
 │    ├── watchdog.py              # Daemon polling prices every 30s during market hours
 │    ├── alerting.py              # OS push-notification alert framework
 │    ├── emergency_executor.py    # Hardcoded liquidation fallbacks for dead engines
 │    └── dashboard.py             # curses-based terminal GUI
 ├── strategy/
 │    ├── runner.py                # Linux subprocess sandbox execution environment wrapper
 │    └── user_strategies/         # Drop-in folder for user-developed alpha scripts
 ├── deploy/
 │    ├── setup_vps.sh             # Linux setup script
 │    └── *.service                # Systemd unit files for auto-restarting daemons
 ├── graphify/
 │    ├── main.py                  # Codebase knowledge graph generator
 │    ├── codebase_knowledge_graph.html  # Interactive visualization (generated)
 │    └── codebase_knowledge_graph.json  # Raw node-link data output (generated)
 ├── scripts/
 │    ├── alpha_benchmarker.py     # YFinance 5-year historical testing framework
 │    └── wait_for_socket.py       # IPC initialization helper
 ├── engine.py                     # Main execution loop connecting strategy, risk, and broker
 └── trade.py                      # CLI entry point wrapper for interaction
```

## Installation

### Prerequisites
*   Python 3.10+
*   Linux VPS (Recommended) or macOS. *(Note: Windows can run tests and paper trades, but live execution daemons require Unix domain sockets).*

### Exact Setup Instructions
1. **Clone the repository**
```bash
git clone <repository_url>
cd trading_platform
```
2. **Create a virtual environment**
```bash
python -m venv venv
source venv/bin/activate
```
3. **Install dependencies**
```bash
pip install -r requirements.txt
pip install pyvis networkx  # Additional dependencies for graphify
```
4. **Configure environment variables**
```bash
cp .env.example .env
# Edit .env and insert your exchange credentials
```
5. **Initialize database**
The SQLite database schema automatically runs migrations on first boot. Trigger it via the CLI:
```bash
python trade.py status
```

## Configuration
The system uses `config/settings.py` to freeze constants. All credentials must exclusively be supplied via the `.env` configuration file exported to environment variables:

*   `ANGEL_API_KEY`: Your Angel One API key
*   `ANGEL_CLIENT_ID`: Your Angel One Client ID
*   `ANGEL_PASSWORD`: Your Angel One client pin/password
*   `ANGEL_TOTP_SECRET`: Your TOTP authentication seed
*   `TRADING_MODE`: Either `paper` or `live`. Fails closed if live keys are missing.
*   `MAX_CAPITAL`: Sets the ceiling allocated to the platform (default 500,000)

## Running the Project
For background autonomous execution, use the provided `start.sh` configuration which sequences the loading of the IPC sockets correctly.

```bash
# Start all platform daemons
./start.sh
```

*(In production, install the systemd daemons and manage them via `systemctl start trading-engine`, `trading-risk`, and `trading-watchdog`)*

### User CLI (`trade.py`)
The `trade.py` script is the primary interface for managing the platform. It communicates with the engine via an HMAC-authenticated Unix domain socket.

| Command | Usage | Description |
| :--- | :--- | :--- |
| **start** | `python trade.py start` | Starts the Trading Engine daemon in the background. |
| **start-watchdog** | `python trade.py start-watchdog` | Starts the Watchdog service for PnL monitoring and stop-loss enforcement. |
| **buy** | `python trade.py buy SYM QTY [limit P]` | Executes a Market or Limit Buy order for the specified symbol. |
| **sell** | `python trade.py sell SYM QTY [limit P]` | Executes a Market or Limit Sell order for the specified symbol. |
| **status** | `python trade.py status` | Returns a JSON object indicating the system health, connectivity, and active services. |
| **positions** | `python trade.py positions` | Lists all current open positions with average price and quantity. |
| **orders** | `python trade.py orders` | Displays a summary of all orders placed during the current session. |
| **reconcile** | `python trade.py reconcile` | Forces the engine to sync local state with the broker's actual book. |
| **kill** | `python trade.py kill "Reason"` | **Emergency Panic Button**. Liquidates all positions and halts all trading. |
| **unkill** | `python trade.py unkill` | Safely removes the kill-switch lock to allow system restart. |
| **stop** | `python trade.py stop` | Sends a graceful shutdown signal to the engine daemon. |
| **dashboard** | `python trade.py dashboard` | Launches the interactive TUI (Terminal User Interface) dashboard. |
| **events** | `python trade.py events` | Tails the structured event log (`events.jsonl`) for real-time debugging. |

---

### Backtesting & Research (`scripts/alpha_benchmarker.py`)
The Alpha Benchmarker is an institutional-grade tool for evaluating your `yfinance` strategies against historical data.

**Basic Usage:**
```bash
python scripts/alpha_benchmarker.py --universe RELIANCE,TCS,INFY --years 2
```

**Advanced Commands:**

*   **Ticker-by-Strategy Matrix**: 
    Evaluate every strategy against every stock in your universe to find the optimal pairing.
    ```bash
    python scripts/alpha_benchmarker.py --universe NIFTY --years 5 --matrix
    ```

*   **Custom Risk Parameters**:
    Test how your strategies survive strict drawdown limits.
    ```bash
    python scripts/alpha_benchmarker.py --universe US --drawdown-limit 0.05
    ```

*   **Arguments Reference:**
    - `--years`: The lookback period for historical data (default: 5).
    - `--universe`: A comma-separated list of tickers (e.g., `AAPL,MSFT`) or pre-defined lists like `NIFTY` or `US`.
    - `--drawdown-limit`: The fractional loss at which the "Circuit Breaker" triggers (default: 0.10 for 10%).
    - `--output-dir`: Where to save images, logs, and comparative charts (default: `backtest_results`).
    - `--matrix`: Enables the granular Ticker-vs-Strategy analysis mode.
    - `--oos`: Run **Out-of-Sample** validation. Splits data 70/30 (In-Sample/Out-of-Sample) to verify if the strategy was overfitted to history.
    - `--wf`: Run **Walk-Forward** validation. Tests the strategy across multiple rolling historical windows to ensure it survives regime shifts.
    - `--mc`: Run **Monte Carlo** simulation. Performs 1,000 randomized permutations of your trade history to calculate "Risk of Ruin" and "Probability of Profit".
    - `--cross-asset`: Run **Cross-Asset** robustness profiler. Tests your strategy against NIFTY, US (S&P 500), and Crypto (BTC/ETH) to see if the edge is global.

**Example Research Flows:**

1. **The Overfit Check (OOS)**:
   ```bash
   python scripts/alpha_benchmarker.py --universe NIFTY --years 3 --oos
   ```

2. **The Stress Test (Monte Carlo)**:
   ```bash
   python scripts/alpha_benchmarker.py --universe NIFTY --years 2 --mc
   ```

3. **Global Robustness (Cross-Asset)**:
   ```bash
   python scripts/alpha_benchmarker.py --cross-asset --years 2
   ```

---

### Codebase Visualization (`graphify`)
To help navigate the backend architecture without reading the entire source code manually, the platform includes a Knowledge Graph generator.

It scans the AST of all Python files (excluding caches and tests) to map out how modules, classes, and methods import and interact with one another.

1. Ensure the visualization dependencies are installed (`pip install pyvis networkx`).
2. Run the graphify tool:
   ```bash
   python graphify/main.py
   ```
3. Open `graphify/codebase_knowledge_graph.html` in any web browser to view the interactive, physics-based network graph of the platform's architecture. A JSON version (`codebase_knowledge_graph.json`) is also generated for downstream tooling.

---

## API Documentation
The platform does not expose an external HTTP REST API, it exposes internal Unix Domain Sockets:

1. **Risk Socket (`/tmp/trading_risk.sock`)**: Listens for TCP JSON-encoded `Signal` inputs. Returning an HTTP `200 Approved` state with no body, or closing the connection upon risk-failure.
2. **Engine Socket (`/tmp/trading_engine.sock`)**: HMAC-SHA256 authenticated IPC for the `trade.py` CLI to send operation codes such as internal `kill`, `positions`, or manual `buy` messages to the Engine Daemon.

## Data Flow
1. **Price Data**: Enters securely via the specific broker `angel.py` adapter. Overwritten and updated continually by the memory-optimized `price_cache.py` which feeds `engine.py` real-time state.
2. **Order Flow Data**: Processed by the `OrderManager` through creation into submission. The broker's UUID tracking IDs are saved strictly into SQLite Table `orders` alongside a global `idempotency_key`. 
3. **Event Persistence**: Immutable data trail is double-written. Transitions are logged to SQLite (`state_transitions` table), whilst human-readable reasoning and rejection reasons from the Risk service are appended asynchronously to `events.jsonl`.

## Execution Pipeline
1. **System Boot**: `start.sh` evaluates network target dependencies and sets up Unix Sockets.
2. **Risk Server Start**: Process 3 initializes and listens on `/tmp/trading_risk.sock`.
3. **Trading Engine Start**: Process 1 initializes, authenticates the broker, decrypts the session JWT, and enters the infinite strategy cycle loop.
4. **Watchdog Start**: Process 4 initializes independently, beginning 30s price polling.
5. **Strategy Subprocess Execution**: The Engine spawns your Python strategy script in an isolated sandbox, pushing the `capital` and `positions` context state through `stdin`.
6. **Signal Generation**: The script processes its logic and identically formats a `BUY/SELL` JSON signal out via `stdout`.
7. **Risk Validation**: The Engine forwards the subprocess signal to the Risk Server socket, evaluating 12 hardcoded pre-trade checks.
8. **Order Submission**: Assuming approval, the Engine Order Manager fires a network request to the Broker API.
9. **Broker Confirmation**: The Broker replies; Order Manager routes the status state to `FILLED`, `REJECTED`, or `UNKNOWN` (on network timeouts).
10. **Position Update**: Database `positions` table increments.
11. **Monitoring and Reconciliation**: Every 5 minutes, the Reconciler forcefully queries the broker over HTTP and overwrites any DB position drift to mathematically lock down truthfulness.

## Strategy Development & Example Usage

Users can drop custom strategy scripts directly into the `strategy/user_strategies/` directory.

### What Does "Submitting a Strategy" Mean?
In this architecture, your trading logic (the "Strategy") is deliberately isolated from the actual broker execution. "Submitting a strategy" simply means placing a Python file inside the `user_strategies` folder and configuring the Engine to run it. 

When the Engine runs your strategy, it:
1. Spawns your script in a **sandbox subprocess** where it cannot access your exchange API keys.
2. Feeds your script the current live market state (capital, open positions, PnL) via `stdin`.
3. Listens to what your script prints to `stdout`.
4. If your script prints a valid `BUY/SELL` JSON signal, the Engine intercepts it, passes it through the 12 Risk Checks, and *only if it passes*, the Engine itself will execute the trade.

This guarantees that a rogue `while True` loop or a buggy logic error in your strategy cannot wipe out your brokerage account.

### How to Build a Strategy
A strategy is just a standard Python script. Here is the minimum required implementation:

```python
import sys
import json

# 1. Read context from the Engine
context = json.loads(sys.stdin.readline())

# 2. Extract data
capital = context.get('capital', 0)
positions = context.get('positions', [])

# 3. Strategy Logic (Example: Buy on Positive Capital)
if capital > 10000:
    # 4. Print your signal (This legally submits the trade proposal to the Risk Engine)
    print(json.dumps({
        "symbol": "RELIANCE",
        "action": "BUY",
        "quantity": 10,
        "order_type": "MARKET"
    }))
```

### Where to Put It & How to Execute It
1. **Save your script**: Save the file (e.g., `my_alpha.py`) inside the `strategy/user_strategies/` folder.
2. **Execute it via the Engine**: The main `engine.py` daemon is responsible for triggering your scripts. Inside `engine.py`, you can configure the `strategy_cycle()` to point to your specific script:
    ```python
    # Inside engine.py
    runner = StrategyRunner("strategy/user_strategies/my_alpha.py", "MyAlpha1")
    ```
3. **Restart your engine**: `./start.sh` (or `systemctl restart trading-engine`).
4. **Monitor execution visually**: 
   ```bash
   python -m monitor.dashboard
   ```

*See `STRATEGY_README.md` for the exact input/output data contract and advanced examples.*

## Logs and Debugging
Logs are segmented explicitly per-process so errors are quickly identified.
*   **engine.log**: Indicates missing environments, strategy subprocess JSON decoding errors (`stdin` parsing failures), or socket failures.
*   **risk_server.log**: Logs which configuration checks were tripped during trade assessment (e.g., `Failed Max Notional`, `Failed Market Hours`).
*   **watchdog.log**: Highlights price divergence checks and when a stop-loss is engaged.

Use the `events.jsonl` file to grep for semantic anomalies, or directly poll the SQLite databases via standard CLI to debug order reconciliation issues:
```bash
sqlite3 trading.db "SELECT * FROM reconciliation_log ORDER BY ts DESC LIMIT 10"
```

## Testing
The repository utilizes Pytest alongside a `PaperBroker` and mocked temporary databases. No live API credentials are required to evaluate test coverage or correctness.

```bash
pip install pytest
python -m pytest tests/ -v -s --tb=short
```

Tests validate critical infrastructure requirements including order lifecycle, DB schema migration idempotency, price deviation detection heuristics, and risk engine block logic.

## Troubleshooting
* **ModuleNotFoundError: 'pandas'**: Reissue pip force reinstall over global namespace dependencies or activate the venv correctly before starting the engine.
* **Platform Won't Start**: Check for a stale `.kill_switch` or `.engine.pid` file generated on improper exits prohibiting boot sequences. `rm .engine.pid`.
* **Database Locked/WinError 1450**: You are using Windows. Concurrent process access inside a WAL SQLite array requires Posix file locking structures. Run this inside WSL / Linux VPS environments.

## Contributing
Contributors should focus solely on enhancing internal `Risk Checks`, expanding upon abstract `broker/base.py` integrations for supplementary brokers (such as Zerodha API or Upstox API), and creating more comprehensive `StrategyRunner` capabilities to abstract non-Pythonic logic scripts (Go/Rust backends). Core domain architecture is finalized and meant specifically to decouple logic routing.



 have added Phase 13: Institutional Execution Upgrades to our implementation_plan.md and task.md so we have a formal roadmap for these top 0.1% features.

Here is exactly how we add them and the immense architectural dangers you must watch out for when we build them:

1. WebSocket Tick Data (broker/angel_ws.py)
How to add it: Angel One provides a SmartAPI WebSocket. We would write a background thread that maintains an open WSS connection and continuously dumps tick data into store/price_cache.py. What to watch out for: WebSockets disconnect. Constantly. If your WebSocket silently drops at 10:45 AM, your system might keep trading on the 10:45 AM price for the rest of the day. You must build auto-reconnect logic with exponential backoff and a "stale tick" validator that throws a fatal error if the last tick was received more than 5 seconds ago.

2. Asyncio Event-Driven Engine (engine_async.py)
How to add it: We replace the while True: time.sleep(CYCLE_TIME) loop in engine.py with an asyncio event loop. It would use asyncio.Queue to listen for tick updates and fire the strategy subprocess immediately the millisecond a tick indicates a breakout. What to watch out for: Python's asyncio is single-threaded. If you accidentally put a synchronous, blocking call inside the event loop (like a heavy SQLite INSERT or a synchronous API request), you will freeze the entire event loop. Your millisecond execution advantage instantly dies. We have to route all database and OS-level IPC calls through a ThreadPoolExecutor or aiosqlite.

3. Smart Order Routing (core/order_manager.py)
How to add it: Instead of submitting "MARKET", the Engine queries the Level 2 top-5 bids and asks. It calculates the Volume Weighted Average Price (VWAP) for your specific quantity. Then it submits a "LIMIT" order pegged exactly to the best Bid. What to watch out for: Sliding limit orders create race conditions. If you bid ₹100.00, and the market jumps to ₹100.50, your order sits unfilled. You have to write logic that checks unfilled limit orders every 3 seconds, cancels them, and recalculates the new limit boundary. Furthermore, if you get partially filled (e.g., 50 shares out of 100), your engine state management has to instantly recognize the partial fill and only cancel/replace the remaining 50 shares to avoid doubling your final position.

Review the updated implementation_plan.md and let me know if you would like me to actually begin writing the code for Phase 13!


For a **sole-operator, single-machine retail architecture**, introducing the Cross-Regime Mean Reversion, Partial Kelly sizing, and Volatility Dispersion modules pushes the mathematical theory of this system into the institutional realm. 

However, as a **top 0.1% quantitative engineer**, I cannot rate you on theory alone. I have to rate the *infrastructure's capacity to execute that theory flawlessly*. 

Here is the brutal, realistic verdict.

---

### The Final Rating: 7.8 / 10

You have built a mathematically beautiful engine that is choking on a fundamentally slow fuel line. 

You have implemented strategies that hedge funds use. The Partial Kelly Criterion protects you from the statistical ruin of "fat-tailed" market events. The Volatility Dispersion strategy ensures you only take delta risk when options markets misprice fear. But the architecture hosting these strategies is bottlenecking their edge.

Here are the fatal discrepancies between your Alpha (the logic you just added) and your Infrastructure (the code running it):

### FLAW 1: The "1D" Data Blindness (The Kelly Sizing Collision)
In the [kelly_sizing.py](cci:7://file:///e:/HFT%20infra/trading_platform/strategy/user_strategies/kelly_sizing.py:0:0-0:0) script, we fetch `interval="1d"` data to compute a 200-SMA. 
**The Brutal Reality:** You are trying to use institutional-grade fractional Kelly sizing on *free, delayed, daily Yahoo Finance data*. This is like trying to fly an F-22 Raptor using a paper map. Kelly Sizing requires absolute precision regarding Risk/Reward. By the time `yfinance` prints a daily close, the institutional volume has already moved the stock 3%, utterly destroying the exact edge (W=0.55, R=1.5) that your Kelly calculation relied upon. 
*For the top 0.1%, Daily (1D) data does not exist for execution. We trade strictly on tick-level Level 2 Order Book updates.*

### FLAW 2: The Options Data Vacuum (The Dispersion Collision)
In [volatility_dispersion.py](cci:7://file:///e:/HFT%20infra/trading_platform/strategy/user_strategies/volatility_dispersion.py:0:0-0:0), you are calculating the Variance Risk Premium. You correctly defined the math to compare Implied Volatility (IV) to Historical Volatility (HV). 
**The Brutal Reality:** `yfinance` options chains for `.NS` (NSE India) are notoriously broken, delayed, or empty. Options pricing relies on Black-Scholes mechanisms that are incredibly sensitive to time-decay (Theta) and interest rates (Rho) measured down to the minute. You cannot calculate accurate Implied Volatility off a free, delayed API. If the IV is stale by even 5 minutes, your Volatility Spread calculation is completely fictitious, and your execution will buy at the exact moment the market maker is dumping their delta hedge.

### FLAW 3: Engine Synchronicity vs. Market Asynchronicity
Your engine is loop-based. It wakes up every X seconds to spawn a strategy process.
**The Brutal Reality:** The market is event-driven; it is discontinuous. A breakout from a multi-year consolidation range (which your [cross_regime_mr.py](cci:7://file:///e:/HFT%20infra/trading_platform/strategy/user_strategies/cross_regime_mr.py:0:0-0:0) tries to identify) happens in milliseconds when an institutional block order clears the Ask. If your engine loop kicks in 14 seconds *after* the breakout, you will suffer catastrophic slippage entering the trade. Top 0.1% platforms are asynchronously interrupt-driven. They don't "loop"; they *react instantly* to a WebSocket streaming tick data.

### FLAW 4: The Order Type Naivety
Your strategies output `"order_type": "MARKET"`. 
**The Brutal Reality:** A top 0.1% firm would fire any execution engineer using a Market Order on an illiquid equity. If your Partial Kelly decides to allocate ₹1,00,000 into a stock, and the order book is thin, your Market order will sweep the book and you will endure 1–2% slippage on entry. That instantly kills the entire edge calculated by the Kelly formula. You *must* implement logic to parse the Bid-Ask spread and submit a Limit Order sitting just inside the bid, dynamically modifying the limit if it isn't filled within 5 seconds.

---

### How to get this to a 9.5 / 10:

You have the right theory. You have built a fortress that perfectly protects you from total loss (The Watchdog, The Kill Switch, The 12 Risk Invariants). Now you must fix the execution leaks:

1. **Delete Yahoo Finance for Live Execution:** Your engine needs a WebSocket integration with Angel One (or TrueData/GoCharting) that streams real-time tick and options data directly into memory (`price_cache.py`).
2. **Transition from Loop to Event-Driven:** The Engine shouldn't sleep. It should be an `asyncio` event loop waiting for a `Tick` object to arrive from the WebSocket to instantly fire the strategy subprocess.
3. **Smart Order Routing:** Never return a `MARKET` order again. Upgrade the [OrderManager](cci:2://file:///e:/HFT%20infra/trading_platform/core/order_manager.py:24:0-284:31) to calculate VWAP and submit sliding Limit Orders.

**Conclusion:** You are building F1 engines and installing them into a Toyota. The math is brilliant, the safety is pristine, but the structural speed and data quality must be upgraded to realize the alpha.


# ── NIFTY (all equivalent) ────────────────────────────────────────────────────
python scripts/alpha_pipeline.py --alpha strategy/user_strategies/multifactor_alpha.py --universe NIFTY
python scripts/alpha_pipeline.py --alpha strategy/user_strategies/multifactor_alpha.py --universe NIFTY50
python scripts/alpha_pipeline.py --alpha strategy/user_strategies/multifactor_alpha.py --universe N50
python scripts/alpha_pipeline.py --alpha strategy/user_strategies/multifactor_alpha.py --universe INDIA

# ── US Large Cap ──────────────────────────────────────────────────────────────
python scripts/alpha_pipeline.py --alpha strategy/user_strategies/multifactor_alpha.py --universe US
python scripts/alpha_pipeline.py --alpha strategy/user_strategies/multifactor_alpha.py --universe SP500

# ── US Tech only ─────────────────────────────────────────────────────────────
python scripts/alpha_pipeline.py --alpha strategy/user_strategies/multifactor_alpha.py --universe US_TECH

# ── Crypto ───────────────────────────────────────────────────────────────────
python scripts/alpha_pipeline.py --alpha strategy/user_strategies/multifactor_alpha.py --universe CRYPTO

# ── Custom NSE tickers (auto-appends .NS) ────────────────────────────────────
python scripts/alpha_pipeline.py --alpha strategy/user_strategies/multifactor_alpha.py --universe "RELIANCE,TCS,INFY,HDFCBANK,ICICIBANK"

# ── Custom US tickers ────────────────────────────────────────────────────────
python scripts/alpha_pipeline.py --alpha strategy/user_strategies/multifactor_alpha.py --universe "AAPL,MSFT,GOOGL,NVDA,TSLA"

# ── Custom Crypto ─────────────────────────────────────────────────────────────
python scripts/alpha_pipeline.py --alpha strategy/user_strategies/multifactor_alpha.py --universe "BTC-USD,ETH-USD,SOL-USD"

# ── Mixed (explicit suffixes required) ───────────────────────────────────────
python scripts/alpha_pipeline.py --alpha strategy/user_strategies/multifactor_alpha.py --universe "RELIANCE.NS,AAPL,BTC-USD"

# ── With extra flags ─────────────────────────────────────────────────────────
# More capital
python scripts/alpha_pipeline.py --alpha strategy/user_strategies/multifactor_alpha.py --universe US --capital 1000000

# Longer backtest
python scripts/alpha_pipeline.py --alpha strategy/user_strategies/multifactor_alpha.py --universe NIFTY --years 5

# Live execution (requires broker)
python scripts/alpha_pipeline.py --alpha strategy/user_strategies/multifactor_alpha.py --universe NIFTY --live

# Combine all flags
python scripts/alpha_pipeline.py --alpha strategy/user_strategies/multifactor_alpha.py --universe NIFTY50 --years 5 --capital 1000000
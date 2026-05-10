#!/bin/bash
# Start all processes with proper synchronization (not sleep).
set -e

echo "Starting trading platform..."

# Verify environment
if [ -z "$ANGEL_API_KEY" ] && [ "${TRADING_MODE:-paper}" != "paper" ]; then
    echo "ERROR: Set ANGEL_API_KEY or use TRADING_MODE=paper"
    exit 1
fi

mkdir -p logs

# 1. Start Risk Server
python risk/server.py >> logs/risk_stdout.log 2>> logs/risk_stderr.log &
RISK_PID=$!
echo $RISK_PID > .risk.pid

# Wait for Risk Server socket — actual synchronization
if ! python scripts/wait_for_socket.py /tmp/trading_risk.sock 10; then
    echo "FATAL: Risk server did not start within 10 seconds"
    cat logs/risk_stderr.log | tail -20
    kill $RISK_PID 2>/dev/null
    rm -f .risk.pid
    exit 1
fi
echo "  ✓ Risk server: PID $RISK_PID [ready]"

# 2. Start Engine
python engine.py >> logs/engine_stdout.log 2>> logs/engine_stderr.log &
ENGINE_PID=$!

# Wait for Engine socket
if ! python scripts/wait_for_socket.py /tmp/trading_engine.sock 15; then
    echo "FATAL: Engine did not start within 15 seconds"
    cat logs/engine_stderr.log | tail -20
    kill $RISK_PID $ENGINE_PID 2>/dev/null
    rm -f .risk.pid
    exit 1
fi
echo "  ✓ Engine: PID $ENGINE_PID [ready]"

# 3. Start Watchdog
python monitor/watchdog.py >> logs/watchdog_stdout.log 2>> logs/watchdog_stderr.log &
WATCHDOG_PID=$!
echo $WATCHDOG_PID > .watchdog.pid
echo "  ✓ Watchdog: PID $WATCHDOG_PID"

echo ""
echo "Platform running. All processes verified."
echo "  Dashboard:  python trade.py dashboard"
echo "  Alerts:     python trade.py tail"
echo "  Events:     python trade.py events"
echo "  Status:     python trade.py status"
echo "  Kill:       python trade.py kill [reason]"
echo "  Stop:       ./stop.sh"

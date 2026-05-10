#!/bin/bash
# Stop all processes cleanly.
echo "Stopping trading platform..."

# Graceful shutdown via socket
python trade.py stop 2>/dev/null || true
sleep 2

# Kill remaining processes via PID files
for pidfile in .risk.pid .watchdog.pid .engine.pid; do
    if [ -f "$pidfile" ]; then
        pid=$(cat "$pidfile" 2>/dev/null)
        if [ -n "$pid" ]; then
            kill "$pid" 2>/dev/null || true
        fi
        rm -f "$pidfile"
    fi
done

# Clean up socket files
rm -f /tmp/trading_engine.sock /tmp/trading_risk.sock

echo "Platform stopped."

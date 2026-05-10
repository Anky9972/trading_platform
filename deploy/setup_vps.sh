#!/bin/bash
# One-shot VPS setup for trading platform.
# Run on a fresh Ubuntu 22.04+ VPS.
# Prerequisites: git clone the repo to current directory first.
set -e

echo "=== Trading Platform VPS Setup ==="

# Check running as root
if [ "$EUID" -ne 0 ]; then
    echo "Run as root: sudo $0"
    exit 1
fi

PLATFORM_DIR="/home/trader/trading_platform"

# Create dedicated non-root trader user
useradd -m -s /bin/bash trader 2>/dev/null || echo "User 'trader' already exists"

# Create environment file — operator fills in credentials
if [ ! -f /home/trader/.trading_env ]; then
    cat > /home/trader/.trading_env << 'EOF'
ANGEL_API_KEY=
ANGEL_CLIENT_ID=
ANGEL_PASSWORD=
ANGEL_TOTP_SECRET=
TRADING_MODE=paper
MAX_CAPITAL=500000
EOF
    chmod 600 /home/trader/.trading_env
    chown trader:trader /home/trader/.trading_env
    echo ""
    echo ">>> EDIT /home/trader/.trading_env with your credentials <<<"
    echo ">>> Then run: sudo $0 --install <<<"
    echo ""
fi

if [ "$1" != "--install" ]; then
    echo "Setup created /home/trader/.trading_env"
    echo "Fill in credentials, then: sudo $0 --install"
    exit 0
fi

echo "Installing platform..."

# Copy code to trader home
cp -r . $PLATFORM_DIR 2>/dev/null || true
chown -R trader:trader $PLATFORM_DIR

# Create Python venv
su -c "python3 -m venv $PLATFORM_DIR/venv" trader
su -c "$PLATFORM_DIR/venv/bin/pip install --quiet -r $PLATFORM_DIR/requirements.txt" trader

# Initialize database
su -c "$PLATFORM_DIR/venv/bin/python -c \"from store.database import Database; Database('$PLATFORM_DIR/trading.db')\"" trader

# Create log directory
su -c "mkdir -p $PLATFORM_DIR/logs" trader

# Install systemd units
cp $PLATFORM_DIR/deploy/trading-risk.service /etc/systemd/system/
cp $PLATFORM_DIR/deploy/trading-engine.service /etc/systemd/system/
cp $PLATFORM_DIR/deploy/trading-watchdog.service /etc/systemd/system/

systemctl daemon-reload
systemctl enable trading-risk trading-engine trading-watchdog

echo ""
echo "=== Installation complete ==="
echo ""
echo "Start services:"
echo "  sudo systemctl start trading-risk"
echo "  sleep 3"
echo "  sudo systemctl start trading-engine"
echo "  sudo systemctl start trading-watchdog"
echo ""
echo "Check status:"
echo "  sudo systemctl status trading-engine"
echo "  sudo journalctl -u trading-engine -f"
echo ""
echo "Check alerts:"
echo "  tail -f $PLATFORM_DIR/alerts.log"
echo ""
echo "Change to LIVE mode (after paper validation):"
echo "  Edit /home/trader/.trading_env: TRADING_MODE=live"
echo "  sudo systemctl restart trading-engine trading-watchdog"

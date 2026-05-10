# Writing a Compatible Strategy

## What the Platform Does For You

The platform handles everything except deciding what to trade:

- Order placement, retry, and idempotency
- Pre-trade risk checks (12 checks including capital limits, daily loss, market hours)
- Position tracking and reconciliation against broker
- Stop loss monitoring every 30 seconds (by Watchdog)
- Emergency sell if engine dies during a stop loss breach
- Full audit trail of every order and fill

## What Your Strategy Must Do

Your strategy receives market context, decides what to trade, and outputs signals.
That is all it does. It does not place orders directly.

## Input: Context on stdin

The platform writes a single JSON object to your strategy's stdin:

```json
{
  "positions": [
    {
      "symbol": "PERSISTENT",
      "quantity": 10,
      "avg_price": 5200.0,
      "last_fill_at": "2025-07-15T10:30:00"
    }
  ],
  "capital": 500000,
  "timestamp": "2025-07-22T19:30:00"
}
```

Read it with:
```python
import sys, json
context = json.loads(sys.stdin.readline())
```

## Output: Signals on stdout

Write one JSON object per line for each signal:

```json
{"symbol": "PERSISTENT", "action": "BUY", "quantity": 10, "order_type": "MARKET", "reason": "PEAD: 15% surprise Q1FY26", "idempotency_key": "pead_PERSISTENT_Q1FY26"}
```

### Required fields

| Field | Values | Notes |
|-------|--------|-------|
| `symbol` | `"PERSISTENT"` | NSE symbol, uppercase, alpha only |
| `action` | `"BUY"` or `"SELL"` | |
| `quantity` | positive integer | Must be > 0, ≤ 5000 |

### Optional fields

| Field | Default | Notes |
|-------|---------|-------|
| `order_type` | `"MARKET"` | `"MARKET"` or `"LIMIT"` |
| `price` | none | Required if `order_type="LIMIT"` |
| `reason` | `""` | Logged for audit trail |
| `idempotency_key` | auto-generated | **You should always provide this** |

## The Idempotency Key — Most Important Field

**Every signal must have a unique idempotency key.**

The key prevents the same logical trade from being placed twice. Generate deterministic keys:

```python
import hashlib
idem_key = hashlib.sha256(f"pead_{symbol}_{quarter}".encode()).hexdigest()[:12]
```

**Never use random UUIDs.**

## Environment Your Strategy Runs In

- **No broker credentials** — stripped from environment
- **Fake HOME** — points to temp sandbox
- **30-second timeout** — killed after 30s
- **Resource limits (Linux)** — 256MB RAM, no forking, 50 FDs

## Template

```python
#!/usr/bin/env python3
import sys, json, hashlib
from datetime import datetime

def main():
    context = json.loads(sys.stdin.readline())
    positions = context.get("positions", [])
    capital = context.get("capital", 500000)
    signals = []

    # YOUR STRATEGY LOGIC HERE

    for sig in signals:
        print(json.dumps(sig))

if __name__ == "__main__":
    main()
```

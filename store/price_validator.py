"""
Secondary price validation using yfinance.
Policy: ALWAYS return the primary price.
yfinance is validation-only. It never overrides the primary.

3-strike rule:
  If broker_price and yfinance_price diverge by >5% for 3 consecutive checks,
  send an operator alert. Possible causes:
    - Broker returning stale/zero prices
    - Stock split or corporate action not reflected
    - Actual flash crash (yfinance source also wrong)
"""
import time
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

try:
    import yfinance as yf
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False
    logger.warning("yfinance not installed — price validation disabled")


PRICE_DIVERGENCE_THRESHOLD = 0.05  # 5%
CONSECUTIVE_ALERT_THRESHOLD = 3
YF_CACHE_TTL = 120  # seconds


def alert(message: str, level: str = "WARNING"):
    """Alert stub — delegates to monitor.alerting when available."""
    try:
        from monitor.alerting import alert as real_alert
        real_alert(message, level)
    except ImportError:
        logger.warning(f"ALERT: {message}")


class PriceValidator:
    def __init__(self, events=None):
        self.events = events
        self._yf_cache = {}        # {symbol: (price, timestamp)}
        self._suspect_counts = {}  # {symbol: consecutive_divergence_count}

    def validate(self, symbol: str, primary_price: float) -> float:
        """
        Validate primary_price against yfinance.
        ALWAYS returns primary_price (or 0 if invalid).
        Side effects: logging + alerting on divergence.
        """
        # Zero/negative price is always invalid
        if primary_price <= 0:
            return 0.0

        if not HAS_YFINANCE:
            return primary_price

        secondary = self._get_yf_price(symbol)
        if secondary is None or secondary <= 0:
            return primary_price

        # Calculate divergence
        divergence = abs(primary_price - secondary) / secondary

        if divergence > PRICE_DIVERGENCE_THRESHOLD:
            count = self._suspect_counts.get(symbol, 0) + 1
            self._suspect_counts[symbol] = count
            logger.warning(
                f"Price divergence: {symbol} broker={primary_price} "
                f"yfinance={secondary} div={divergence:.1%} "
                f"(consecutive #{count})"
            )

            if self.events:
                from store.event_log import EventType
                self.events.emit(EventType.PRICE_VALIDATION_FAILED, {
                    "symbol": symbol,
                    "broker_price": primary_price,
                    "yfinance_price": secondary,
                    "divergence_pct": round(divergence * 100, 2),
                    "consecutive_count": count,
                })

            if count >= CONSECUTIVE_ALERT_THRESHOLD:
                alert(
                    f"🔴 PRICE VALIDATION: {symbol} has diverged >{PRICE_DIVERGENCE_THRESHOLD*100}% "
                    f"for {count} consecutive checks. "
                    f"Broker={primary_price} yfinance={secondary}",
                    level="CRITICAL"
                )
        else:
            # Good reading — reset counter
            self._suspect_counts[symbol] = 0

        # ALWAYS return primary
        return primary_price

    def _get_yf_price(self, symbol: str) -> float:
        """Get price from yfinance with short-term caching."""
        now = time.time()

        if symbol in self._yf_cache:
            cached_price, cached_time = self._yf_cache[symbol]
            if now - cached_time < YF_CACHE_TTL:
                return cached_price

        try:
            # NSE symbols need .NS suffix for yfinance
            yf_symbol = f"{symbol}.NS"
            ticker = yf.Ticker(yf_symbol)
            info = ticker.fast_info
            price = getattr(info, 'last_price', None)
            if price and price > 0:
                self._yf_cache[symbol] = (price, now)
                return price
        except Exception as e:
            logger.debug(f"yfinance lookup failed for {symbol}: {e}")

        return None

"""
Angel One broker adapter.
Uses SmartAPI Python client.
Session persistence to avoid rate-limited re-auth.
Circuit breaker for consecutive failures.
Rate limiting (1 request per second).
"""
import os
import time
import json
import logging
from datetime import datetime
from typing import Optional
from core.models import Signal, Action, OrderType
from core.session_store import SessionStore
from broker.base import (
    BrokerAdapter, BrokerError, BrokerAuthError,
    BrokerTimeoutError, BrokerRateLimitError, BrokerRejectionError,
)
from config.settings import SETTINGS

logger = logging.getLogger(__name__)


class AngelBroker(BrokerAdapter):
    def __init__(self):
        self._smart_api = None
        self._session = None
        self._session_store = SessionStore()
        self._connected = False
        self._last_request_time = 0
        self._min_interval = 1.0     # 1 second between API calls

        # Circuit breaker
        self._consecutive_failures = 0
        self._circuit_open = False
        self._circuit_open_until = 0
        self._failure_threshold = 5  # Open after 5 consecutive failures
        self._reset_timeout = 60     # Close after 60 seconds

        # Symbol map: readable name → exchange token
        self._symbol_map = {}

    def connect(self) -> None:
        """Connect to Angel One. Try saved session first, then fresh login."""
        try:
            from SmartApi import SmartConnect
        except ImportError:
            raise BrokerError(
                "SmartApi not installed. Run: pip install smartapi-python"
            )

        self._smart_api = SmartConnect(api_key=SETTINGS.ANGEL_API_KEY)

        # Try to restore saved session
        saved = self._session_store.load()
        if saved:
            self._smart_api.setAccessToken(saved.get("access_token", ""))
            self._smart_api.setRefreshToken(saved.get("refresh_token", ""))
            self._session = saved

            # Validate session with a lightweight call
            try:
                profile = self._api_call(self._smart_api.getProfile,
                                         SETTINGS.ANGEL_CLIENT_ID)
                if profile and profile.get('status'):
                    self._connected = True
                    logger.info("Restored saved session")
                    self._load_symbol_map()
                    return
            except Exception:
                logger.info("Saved session expired, doing fresh login")

        # Fresh login
        self._fresh_login()

    def _fresh_login(self):
        """Full authentication flow. Session is saved for next restart."""
        import pyotp

        totp = pyotp.TOTP(SETTINGS.ANGEL_TOTP_SECRET)
        totp_value = totp.now()

        try:
            result = self._smart_api.generateSession(
                SETTINGS.ANGEL_CLIENT_ID,
                SETTINGS.ANGEL_PASSWORD,
                totp_value
            )
        except Exception as e:
            raise BrokerAuthError(f"Angel One login failed: {e}")

        if not result or not result.get('status'):
            msg = result.get('message', 'Unknown error') if result else 'No response'
            raise BrokerAuthError(f"Angel One login failed: {msg}")

        self._session = result['data']
        self._connected = True

        # Save session for next restart
        self._session['_saved_at'] = datetime.now().isoformat()
        self._session_store.save(self._session)

        logger.info("Fresh login successful. Session saved.")
        self._load_symbol_map()

    def _load_symbol_map(self):
        """Load Angel One symbol mapping (token → tradingsymbol)."""
        try:
            if os.path.exists("angel_symbols.json"):
                with open("angel_symbols.json") as f:
                    self._symbol_map = json.load(f)
        except Exception:
            pass

    def is_connected(self) -> bool:
        return self._connected

    def _api_call(self, func, *args, **kwargs):
        """
        Central API call wrapper with:
        - Rate limiting (1 req/sec)
        - Circuit breaker
        - Error classification
        """
        # Circuit breaker check
        if self._circuit_open:
            if time.time() < self._circuit_open_until:
                raise BrokerError("Circuit breaker is OPEN — too many failures")
            else:
                self._circuit_open = False
                self._consecutive_failures = 0
                logger.info("Circuit breaker half-open — attempting recovery")

        # Rate limiting
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

        try:
            self._last_request_time = time.time()
            result = func(*args, **kwargs)
            self._consecutive_failures = 0  # Reset on success
            return result

        except Exception as e:
            self._consecutive_failures += 1
            err_str = str(e).lower()

            # Open circuit breaker after N failures
            if self._consecutive_failures >= self._failure_threshold:
                self._circuit_open = True
                self._circuit_open_until = time.time() + self._reset_timeout
                logger.critical(
                    f"Circuit breaker OPEN after {self._consecutive_failures} failures"
                )

            # Classify error
            if "unauthorized" in err_str or "401" in err_str or "403" in err_str:
                raise BrokerAuthError(str(e)) from e
            elif "timeout" in err_str or "timed out" in err_str:
                raise BrokerTimeoutError(str(e)) from e
            elif "429" in err_str or "rate" in err_str:
                raise BrokerRateLimitError(str(e)) from e
            else:
                raise BrokerError(str(e)) from e

    def place_order(self, signal: Signal) -> str:
        """Place order via Angel One SmartAPI."""
        if not self._connected:
            raise BrokerError("Not connected to Angel One")

        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": signal.symbol,
            "symboltoken": self._symbol_map.get(signal.symbol, ""),
            "transactiontype": signal.action.value,
            "exchange": "NSE",
            "ordertype": signal.order_type.value,
            "producttype": "DELIVERY",
            "duration": "DAY",
            "quantity": str(signal.quantity),
        }

        if signal.price is not None:
            order_params["price"] = str(signal.price)
        else:
            order_params["price"] = "0"

        if signal.trigger_price is not None:
            order_params["triggerprice"] = str(signal.trigger_price)

        try:
            result = self._api_call(self._smart_api.placeOrder, order_params)
        except BrokerTimeoutError:
            raise  # Let caller handle — UNKNOWN state
        except BrokerRateLimitError:
            raise  # Let caller handle — retry after delay
        except Exception as e:
            raise BrokerError(f"Order placement failed: {e}") from e

        if not result or not result.get('status'):
            msg = result.get('message', 'Unknown') if result else 'No response'
            raise BrokerRejectionError(f"Broker rejected: {msg}")

        broker_order_id = result.get('data', {}).get('orderid', '')
        if not broker_order_id:
            raise BrokerError("No order ID in broker response")

        return broker_order_id

    def get_order_status(self, broker_order_id: str) -> dict:
        """Get status of a specific order."""
        try:
            book = self._api_call(self._smart_api.orderBook)
        except Exception as e:
            raise BrokerError(f"Cannot get order status: {e}") from e

        if not book or not book.get('data'):
            return {"status": "not_found", "filledshares": "0",
                    "averageprice": "0", "text": "Order book empty"}

        for order in book['data']:
            if order.get('orderid') == broker_order_id:
                return {
                    "status": order.get('status', '').lower(),
                    "filledshares": order.get('filledshares', '0'),
                    "averageprice": order.get('averageprice', '0'),
                    "text": order.get('text', ''),
                }

        return {"status": "not_found", "filledshares": "0",
                "averageprice": "0", "text": "Order not in book"}

    def get_order_book(self) -> list:
        try:
            result = self._api_call(self._smart_api.orderBook)
            return result.get('data', []) if result else []
        except Exception:
            return []

    def get_holdings(self) -> list:
        try:
            result = self._api_call(self._smart_api.holding)
            return result.get('data', []) if result else []
        except Exception as e:
            raise BrokerError(f"Cannot get holdings: {e}") from e

    def get_ltp(self, symbol: str) -> float:
        try:
            token = self._symbol_map.get(symbol, "")
            result = self._api_call(
                self._smart_api.ltpData, "NSE", symbol, token
            )
            if result and result.get('data'):
                return float(result['data'].get('ltp', 0))
        except Exception as e:
            logger.warning(f"LTP fetch failed for {symbol}: {e}")
        return 0.0

    def get_ltp_batch(self, symbols: list) -> dict:
        """Fetch LTP for multiple symbols."""
        prices = {}
        for symbol in symbols:
            try:
                prices[symbol] = self.get_ltp(symbol)
            except Exception:
                prices[symbol] = 0.0
        return prices

    def get_funds(self) -> dict:
        try:
            result = self._api_call(self._smart_api.rmsLimit)
            if result and result.get('data'):
                data = result['data']
                return {
                    "available_cash": float(data.get('availablecash', 0)),
                    "used_margin": float(data.get('utiliseddebits', 0)),
                }
        except Exception as e:
            logger.warning(f"Funds fetch failed: {e}")
        return {"available_cash": float(SETTINGS.MAX_CAPITAL)}

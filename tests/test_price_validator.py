"""
Tests for store/price_validator.py

Covers:
- Normal price passthrough
- Zero/negative price rejection
- Divergence detection
- Consecutive alert threshold
"""
import pytest
from unittest.mock import patch, MagicMock
from store.price_validator import PriceValidator, PRICE_DIVERGENCE_THRESHOLD
from store.event_log import EventLog


class TestPriceValidator:
    def test_returns_primary_price_always(self, tmp_events):
        pv = PriceValidator(events=tmp_events)
        # Even without yfinance, primary is returned
        result = pv.validate("PERSISTENT", 5200.0)
        assert result == 5200.0

    def test_zero_price_returns_zero(self, tmp_events):
        pv = PriceValidator(events=tmp_events)
        result = pv.validate("PERSISTENT", 0.0)
        assert result == 0.0

    def test_negative_price_returns_zero(self, tmp_events):
        pv = PriceValidator(events=tmp_events)
        result = pv.validate("PERSISTENT", -100.0)
        assert result == 0.0

    @patch('store.price_validator.HAS_YFINANCE', True)
    def test_divergence_increments_suspect_count(self, tmp_events):
        pv = PriceValidator(events=tmp_events)
        # Simulate yfinance returning a different price
        pv._yf_cache["PERSISTENT"] = (4000.0, __import__('time').time())

        result = pv.validate("PERSISTENT", 5200.0)
        assert result == 5200.0  # Always returns primary
        assert pv._suspect_counts.get("PERSISTENT", 0) == 1

    @patch('store.price_validator.HAS_YFINANCE', True)
    def test_good_reading_resets_suspect_count(self, tmp_events):
        pv = PriceValidator(events=tmp_events)
        pv._suspect_counts["PERSISTENT"] = 2
        # Simulate matching price
        pv._yf_cache["PERSISTENT"] = (5200.0, __import__('time').time())

        result = pv.validate("PERSISTENT", 5200.0)
        assert result == 5200.0
        assert pv._suspect_counts.get("PERSISTENT", 0) == 0

"""
Tests for strategy/runner.py

Covers:
- Subprocess isolation (credentials stripped)
- Signal validation (symbol format, action enum)
- Timeout enforcement
- JSON output parsing
"""
import pytest
import os
import sys
import tempfile
from strategy.runner import StrategyRunner


class TestStrategyRunnerExecution:
    def _create_strategy_file(self, tmp_path, code: str) -> str:
        """Write a temporary strategy Python file."""
        strategy_file = str(tmp_path / "test_strategy.py")
        with open(strategy_file, 'w') as f:
            f.write(code)
        return strategy_file

    def test_valid_strategy_returns_signals(self, tmp_path):
        """Strategy that outputs valid BUY signal."""
        code = '''
import sys, json
context = json.loads(sys.stdin.readline())
print(json.dumps({"symbol": "PERSISTENT", "action": "BUY", "quantity": 10, "reason": "test"}))
'''
        strategy_file = self._create_strategy_file(tmp_path, code)
        runner = StrategyRunner(strategy_file, "test_strategy", timeout_seconds=10)

        signals = runner.run({"positions": [], "capital": 500000})
        assert len(signals) == 1
        assert signals[0].symbol == "PERSISTENT"
        assert signals[0].action.value == "BUY"
        assert signals[0].quantity == 10

    def test_timeout_returns_empty_signals(self, tmp_path):
        """Strategy that hangs beyond timeout gets killed."""
        code = '''
import time
time.sleep(100)
'''
        strategy_file = self._create_strategy_file(tmp_path, code)
        runner = StrategyRunner(strategy_file, "hang_strategy", timeout_seconds=2)

        signals = runner.run({"positions": [], "capital": 500000})
        assert signals == []

    def test_invalid_symbol_rejected(self, tmp_path):
        """Signals with invalid symbols are not returned."""
        code = '''
import sys, json
context = json.loads(sys.stdin.readline())
print(json.dumps({"symbol": "bad symbol!", "action": "BUY", "quantity": 10}))
print(json.dumps({"symbol": "VALID", "action": "BUY", "quantity": 10}))
'''
        strategy_file = self._create_strategy_file(tmp_path, code)
        runner = StrategyRunner(strategy_file, "bad_symbol", timeout_seconds=10)

        signals = runner.run({"positions": [], "capital": 500000})
        assert len(signals) == 1
        assert signals[0].symbol == "VALID"

    def test_invalid_action_rejected(self, tmp_path):
        """Signals with invalid action are not returned."""
        code = '''
import sys, json
context = json.loads(sys.stdin.readline())
print(json.dumps({"symbol": "PERSISTENT", "action": "HOLD", "quantity": 10}))
'''
        strategy_file = self._create_strategy_file(tmp_path, code)
        runner = StrategyRunner(strategy_file, "bad_action", timeout_seconds=10)

        signals = runner.run({"positions": [], "capital": 500000})
        assert len(signals) == 0

    def test_zero_quantity_rejected(self, tmp_path):
        code = '''
import sys, json
context = json.loads(sys.stdin.readline())
print(json.dumps({"symbol": "PERSISTENT", "action": "BUY", "quantity": 0}))
'''
        strategy_file = self._create_strategy_file(tmp_path, code)
        runner = StrategyRunner(strategy_file, "zero_qty", timeout_seconds=10)

        signals = runner.run({"positions": [], "capital": 500000})
        assert len(signals) == 0

    def test_nonzero_exit_returns_empty(self, tmp_path):
        """Strategy that crashes returns no signals."""
        code = '''
import sys
sys.exit(1)
'''
        strategy_file = self._create_strategy_file(tmp_path, code)
        runner = StrategyRunner(strategy_file, "crash_strategy", timeout_seconds=10)

        signals = runner.run({"positions": [], "capital": 500000})
        assert signals == []

    def test_environment_stripped_of_credentials(self, tmp_path):
        """Strategy subprocess should not have broker credentials."""
        code = '''
import sys, json, os
context = json.loads(sys.stdin.readline())
api_key = os.environ.get("ANGEL_API_KEY", "NOT_SET")
if api_key == "NOT_SET":
    print(json.dumps({"symbol": "SAFE", "action": "BUY", "quantity": 1, "reason": "no_creds"}))
else:
    print(json.dumps({"symbol": "UNSAFE", "action": "BUY", "quantity": 1, "reason": "has_creds"}))
'''
        strategy_file = self._create_strategy_file(tmp_path, code)
        runner = StrategyRunner(strategy_file, "cred_test", timeout_seconds=10)

        signals = runner.run({"positions": [], "capital": 500000})
        assert len(signals) == 1
        assert signals[0].symbol == "SAFE"

    def test_sandbox_cleanup(self, tmp_path):
        """Sandbox directory is cleaned up after run."""
        code = '''
import sys, json
context = json.loads(sys.stdin.readline())
print(json.dumps({"symbol": "PERSISTENT", "action": "BUY", "quantity": 1}))
'''
        strategy_file = self._create_strategy_file(tmp_path, code)
        runner = StrategyRunner(strategy_file, "cleanup_test", timeout_seconds=10)

        signals = runner.run({"positions": [], "capital": 500000})
        assert len(signals) == 1
        # Sandbox should be cleaned up
        assert runner._sandbox_dir is not None
        assert not os.path.exists(runner._sandbox_dir)

"""
Tests for monitor/emergency_executor.py

Covers:
- All activation conditions (engine dead + stop loss + no kill switch + daily limit)
- Kill switch always activated after execution
- Daily sell limit enforcement
"""
import pytest
import os
from datetime import datetime
from monitor.emergency_executor import EmergencyExecutor, MAX_EMERGENCY_SELLS_PER_DAY
from config.settings import SETTINGS


class TestActivationConditions:
    def test_should_not_execute_if_engine_alive(self, tmp_db, tmp_events, paper_broker, tmp_path):
        ks_file = str(tmp_path / ".kill_switch")
        object.__setattr__(SETTINGS, 'KILL_SWITCH_FILE', ks_file)
        em = EmergencyExecutor(tmp_db, paper_broker, tmp_events)
        assert em.should_execute("PERSISTENT", -0.10, engine_alive=True) is False

    def test_should_not_execute_if_loss_below_threshold(self, tmp_db, tmp_events, paper_broker, tmp_path):
        ks_file = str(tmp_path / ".kill_switch")
        object.__setattr__(SETTINGS, 'KILL_SWITCH_FILE', ks_file)
        em = EmergencyExecutor(tmp_db, paper_broker, tmp_events)
        assert em.should_execute("PERSISTENT", -0.05, engine_alive=False) is False

    def test_should_not_execute_if_kill_switch_already_active(self, tmp_db, tmp_events, paper_broker, tmp_path):
        ks_file = str(tmp_path / ".kill_switch")
        object.__setattr__(SETTINGS, 'KILL_SWITCH_FILE', ks_file)
        with open(ks_file, 'w') as f:
            f.write("already active\n")
        em = EmergencyExecutor(tmp_db, paper_broker, tmp_events)
        assert em.should_execute("PERSISTENT", -0.10, engine_alive=False) is False

    def test_should_execute_when_all_conditions_met(self, tmp_db, tmp_events, paper_broker, tmp_path):
        ks_file = str(tmp_path / ".kill_switch")
        object.__setattr__(SETTINGS, 'KILL_SWITCH_FILE', ks_file)
        em = EmergencyExecutor(tmp_db, paper_broker, tmp_events)
        assert em.should_execute("PERSISTENT", -0.10, engine_alive=False) is True


class TestExecution:
    def test_sell_places_order_and_activates_kill_switch(self, tmp_db, tmp_events, paper_broker, tmp_path):
        ks_file = str(tmp_path / ".kill_switch")
        object.__setattr__(SETTINGS, 'KILL_SWITCH_FILE', ks_file)

        em = EmergencyExecutor(tmp_db, paper_broker, tmp_events)
        result = em.execute_emergency_sell(
            "PERSISTENT", 10, 4700.0, 5200.0, -0.096
        )
        assert result is True
        assert os.path.exists(ks_file)  # Kill switch activated

    def test_daily_limit_enforced(self, tmp_db, tmp_events, paper_broker, tmp_path):
        ks_file = str(tmp_path / ".kill_switch")
        object.__setattr__(SETTINGS, 'KILL_SWITCH_FILE', ks_file)
        em = EmergencyExecutor(tmp_db, paper_broker, tmp_events)

        for i in range(MAX_EMERGENCY_SELLS_PER_DAY):
            if os.path.exists(ks_file):
                os.unlink(ks_file)
            em.execute_emergency_sell(
                "PERSISTENT", 10, 4700.0, 5200.0, -0.10
            )

        # After max sells, should not execute anymore
        if os.path.exists(ks_file):
            os.unlink(ks_file)
        assert em.should_execute("PERSISTENT", -0.10, engine_alive=False) is False

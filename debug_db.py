import os
import sys
import logging
from datetime import datetime

logging.basicConfig(level=logging.ERROR)

from store.database import Database
from store.event_log import EventLog
from broker.paper import PaperBroker
from monitor.emergency_executor import EmergencyExecutor

def run_debug():
    if os.path.exists("test_debug.db"):
        os.remove("test_debug.db")
    if os.path.exists("test_debug.log"):
        os.remove("test_debug.log")
        
    db = Database("test_debug.db")
    broker = PaperBroker()
    broker.connect()
    broker.set_price("PERSISTENT", 5200.0)
    events = EventLog("test_debug.log")
    
    # Patched to raise instead of swallow
    original_insert = db.insert_order
    def raising_insert(data):
        try:
            original_insert(data)
        except Exception as e:
            print(f"INSERT FAILURE: {repr(e)}")
            raise
    db.insert_order = raising_insert
    
    em = EmergencyExecutor(db, broker, events)
    try:
        em.execute_emergency_sell("PERSISTENT", 10, 4700.0, 5200.0, -0.096)
        print("SUCCESS")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"FAILED: {repr(e)}")

if __name__ == "__main__":
    run_debug()

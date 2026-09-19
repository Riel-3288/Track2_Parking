import os
import threading

# ==============================================================================
# CONFIGURATION & ABSOLUTE PATHS
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "parking.db")
USERS_FILE = os.path.join(BASE_DIR, "users.json")

SIMULATOR_BASE_URL = "http://127.0.0.1:9898/api/v1"
ADMIN_NAME = "admin"
ADMIN_PASS = "admin"

# ==============================================================================
# GLOBAL STATE & LOCKS
# ==============================================================================
jwt_token = None
auth_lock = threading.Lock()
state_lock = threading.Lock()

processed_event_ids = set()
active_cars = {}          # { plate: { "spot": str, "type": str, "duration": int, "expected_cost": float, "charged": bool } }
reserved_spots = set()    # Bays reserved by cars driving to them

entry_gate_name = "gate1"
exit_gate_name = "gate2"
barriers = []
exhaust_fans = []
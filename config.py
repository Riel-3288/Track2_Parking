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

STATIC_ZONE_FANS = {
    "ZONE1": ["f_0", "f_1", "fan4", "fan5"],
    "ZONE2": ["fan0", "fan1", "fan2", "fan3"],
    "ZONE3": ["fan6", "fan7", "fan8", "fan9"],
}
CO_POLL_INTERVAL_SECONDS = 15


# ==============================================================================
# GLOBAL STATE & LOCKS
# ==============================================================================
jwt_token = None
auth_lock = threading.Lock()
state_lock = threading.Lock()

processed_event_ids = set()
active_cars = {}          # { plate: { "spot": str, "type": str, "duration": int, "expected_cost": float, "charged": bool } }
zone_fans_map = {}
zone_fan_state = {}   # { "ZONE1": True/False } — tracks whether that zone's fans are currently ON, to avoid redundant API calls
reserved_spots = set()    # Bays reserved by cars driving to them

entry_gate_name = "gate1"
exit_gate_name = "gate2"
barriers = []
exhaust_fans = []

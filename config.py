import os
import threading

WEBHOOK_REQUIRE_SIGNATURE = True  # require signature verification for incoming webhooks

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

zone_gates = {
    "ZONE1": {
        "entry": "gate1",
        "exit": "gate2"
    },
    "ZONE2": {
        "entry": "gate3",
        "exit": "gate4"
    },
    "ZONE3": {
        "entry": "gate5",
        "exit": "gate6"
    }
}
exhaust_fans = []
co_danger_level = "Safe"   # tracks latest CO2 danger reading
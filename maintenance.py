import time
import threading
import sqlite3
import re
import requests
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
import json
import os

app = Flask(__name__)
app.secret_key = "ctrl_alt_everything_super_secret_key"

USERS_FILE = "users.json"

def load_users():
    if not os.path.exists(USERS_FILE):
        default_users = {
            "admin": {"password": "admin", "role": "admin"},
            "operator": {"password": "operator", "role": "operator"}
        }
        save_users(default_users)
        return default_users
    with open(USERS_FILE, 'r') as f:
        return json.load(f)

def save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=4)

# ==============================================================================
# CONFIGURATION & ZONE LAYOUT
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "parking.db")

SIMULATOR_BASE_URL = "http://127.0.0.1:9898/api/v1"
ADMIN_NAME = "admin"
ADMIN_PASS = "admin"

MAIN_INTAKE_GATE = "gate7"

ZONE_CONFIG = {
    1: {"entry_gate": "gate1", "exit_gate": "gate2", "entry_spot": "ENTRY1", "spot_prefix": "s"},
    2: {"entry_gate": "gate3", "exit_gate": "gate4", "entry_spot": "ENTRY2", "spot_prefix": "bay"},
    3: {"entry_gate": "gate5", "exit_gate": "gate6", "entry_spot": "ENTRY3", "spot_prefix": "p"}
}

ZONE_ENTRY_GATES = {"gate1": 1, "gate3": 2, "gate5": 3}
ZONE_EXIT_GATES = {"gate2": 1, "gate4": 2, "gate6": 3}

# Maintenance Thresholds
MAINTENANCE_THRESHOLDS = {
    "spot_cycles": 2,        # Service bay after 2 parkings
    "gate_cycles": 7,        # Service gate after 7 operations
    "fan_run_seconds": 2700,  # Service fan after 45min 
    "light_run_seconds":2700  # Service light after 45min
}

jwt_token = None
auth_lock = threading.Lock()
state_lock = threading.Lock()

# Live State Tracking
processed_event_ids = set()
active_cars = {}
reserved_spots = set()
moving_cars_per_zone = {1: set(), 2: set(), 3: set()}
co_levels_per_zone = {1: 0.0, 2: 0.0, 3: 0.0}

active_gate_repairs = set()     # Gates currently under maintenance
in_transit_entries = set()      # Prevents 2 cars heading to the same entry box
gate_wants_maintenance = set()  # Gates scheduled for service
pending_spot_repairs = set()
gate_close_timers = {}

is_night_time = False
hardware_inventory = {
    "fans": {},
    "lights": {},
    "gates": {},
    "spots": {}
}

# ==============================================================================
# DATABASE SETUP & USAGE LOGGING
# ==============================================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS car_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plate TEXT, car_type TEXT, spot_name TEXT, zone INTEGER,
        entry_time TEXT, exit_time TEXT,
        parking_cost REAL, charging_cost REAL, total_paid REAL, status TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS penalty_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reason TEXT, fine_amount REAL, timestamp TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS component_usage (
        component_name TEXT PRIMARY KEY,
        component_type TEXT,
        zone INTEGER,
        usage_cycles INTEGER DEFAULT 0,
        run_seconds REAL DEFAULT 0,
        last_maintenance TEXT
    )''')
    conn.commit()
    conn.close()

def reset_all_session_cycles():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("UPDATE component_usage SET usage_cycles = 0, run_seconds = 0")
    conn.commit()
    conn.close()
    print("[INIT] Session cycles reset for clean test run.")

def record_component_cycle(name, comp_type, zone):
    norm = normalize_gate_name(name)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO component_usage (component_name, component_type, zone, usage_cycles, run_seconds, last_maintenance)
                 VALUES (?, ?, ?, 1, 0, datetime('now', 'localtime'))
                 ON CONFLICT(component_name) DO UPDATE SET 
                 usage_cycles = usage_cycles + 1''', (norm, comp_type, zone))
    conn.commit()
    conn.close()

def record_component_runtime(name, comp_type, zone, added_seconds):
    norm = normalize_gate_name(name)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO component_usage (component_name, component_type, zone, usage_cycles, run_seconds, last_maintenance)
                 VALUES (?, ?, ?, 1, ?, datetime('now', 'localtime'))
                 ON CONFLICT(component_name) DO UPDATE SET 
                 usage_cycles = usage_cycles + 1,
                 run_seconds = run_seconds + ?''', (norm, comp_type, zone, added_seconds, added_seconds))
    conn.commit()
    conn.close()

def reset_component_maintenance(name):
    norm = normalize_gate_name(name)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''UPDATE component_usage 
                 SET usage_cycles = 0, run_seconds = 0, last_maintenance = datetime('now', 'localtime')
                 WHERE component_name = ?''', (norm,))
    conn.commit()
    conn.close()

def get_component_cycles(name):
    norm = normalize_gate_name(name)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT usage_cycles FROM component_usage WHERE component_name = ?", (norm,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def log_car_entry(plate, car_type, spot, zone):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO car_logs (plate, car_type, spot_name, zone, entry_time, status)
                 VALUES (?, ?, ?, ?, datetime('now', 'localtime'), 'Parked')''',
              (plate.strip(), car_type, spot, zone))
    conn.commit()
    conn.close()

def log_car_exit(plate, p_cost, c_cost, total):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    clean_plate = plate.strip()
    c.execute('''SELECT id FROM car_logs 
                 WHERE TRIM(UPPER(plate)) = TRIM(UPPER(?)) 
                 ORDER BY id DESC LIMIT 1''', (clean_plate,))
    row = c.fetchone()
    if row:
        c.execute('''UPDATE car_logs 
                     SET exit_time = datetime('now', 'localtime'),
                         parking_cost = ?, charging_cost = ?, total_paid = ?, status = 'Completed'
                     WHERE id = ?''', (p_cost, c_cost, total, row[0]))
    else:
        c.execute('''INSERT INTO car_logs (plate, car_type, spot_name, zone, entry_time, exit_time, parking_cost, charging_cost, total_paid, status)
                     VALUES (?, 'Normal', 'Exit', 1, datetime('now', '-2 minutes', 'localtime'), datetime('now', 'localtime'), ?, ?, ?, 'Completed')''',
                  (clean_plate, p_cost, c_cost, total))
    conn.commit()
    conn.close()

def log_penalty(reason, fine):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO penalty_logs (reason, fine_amount, timestamp)
                 VALUES (?, ?, datetime('now', 'localtime'))''', (reason, fine))
    conn.commit()
    conn.close()

# ==============================================================================
# SIMULATOR API CLIENT
# ==============================================================================
def login_to_simulator():
    global jwt_token
    with auth_lock:
        url = f"{SIMULATOR_BASE_URL}/auth/login"
        payload = {"Email": ADMIN_NAME, "Password": ADMIN_PASS}
        try:
            res = requests.post(url, json=payload, timeout=5)
            if res.status_code == 200:
                jwt_token = res.json().get("token")
                return True
        except Exception as e:
            print(f"[AUTH ERROR] {e}")
        return False

def call_simulator_api(method, endpoint, payload=None, params=None):
    global jwt_token
    if not jwt_token and not login_to_simulator():
        return None

    headers = {"Authorization": f"Bearer {jwt_token}", "Content-Type": "application/json"}
    url = f"{SIMULATOR_BASE_URL}{endpoint}"

    try:
        if method.upper() == "GET":
            res = requests.get(url, headers=headers, params=params, timeout=5)
        else:
            res = requests.post(url, headers=headers, json=payload or {}, params=params, timeout=5)

        if res.status_code == 401:
            if login_to_simulator():
                headers["Authorization"] = f"Bearer {jwt_token}"
                res = requests.request(method, url, headers=headers, json=payload, params=params, timeout=5)

        return res.json() if res.content and res.status_code in [200, 201] else {}
    except Exception as e:
        print(f"[API ERROR] {endpoint}: {e}")
        return None

def is_spot_empty(spot):
    detected = spot.get("detectedCars")
    if detected is None: return True
    if isinstance(detected, int): return detected == 0
    if isinstance(detected, (list, tuple)): return len(detected) == 0
    return False

def parse_zone(val):
    if val is None: return 1
    digits = re.findall(r'\d+', str(val))
    return int(digits[0]) if digits else 1

def normalize_gate_name(name):
    return re.sub(r'[\s_-]+', '', str(name)).lower()

def get_barrier_status(gate_name):
    barriers = call_simulator_api("GET", "/list-barriers") or []
    target = normalize_gate_name(gate_name)
    for b in barriers:
        if normalize_gate_name(b.get("name", "")) == target:
            return b
    return None

def is_gate_operational(gate_name):
    norm = normalize_gate_name(gate_name)
    if norm in active_gate_repairs or norm in gate_wants_maintenance:
        return False

    b = get_barrier_status(gate_name)
    if not b: return False

    cycles = get_component_cycles(norm)
    if cycles >= MAINTENANCE_THRESHOLDS["gate_cycles"]:
        gate_wants_maintenance.add(norm)
        return False

    st = str(b.get("state", "")).lower()
    if st in ["repairing", "maintenance", "undermaintenance", "broken"]:
        return False

    if b.get("broken") or b.get("isUnderMaintenance") or b.get("underMaintenance") or b.get("isRepairing"):
        return False

    return True

# ==============================================================================
# PREFIX-BASED ZONE RESOLVER
# ==============================================================================
def get_spot_zone(s):
    raw_z = s.get("zone") if s.get("zone") is not None else s.get("Zone")
    if raw_z is not None:
        digits = re.findall(r'\d+', str(raw_z))
        if digits:
            return int(digits[0])

    name = str(s.get("name", "")).strip().lower()
    if name.startswith("bay"):
        return 2
    elif name.startswith("p"):
        return 3
    elif name.startswith("s"):
        return 1

    digits = re.findall(r'\d+', name)
    if digits:
        num = int(digits[0])
        if num <= 30: return 1
        elif num <= 60: return 2
        else: return 3
    return 1

def get_entry_spot_exact_name(zone):
    spots = call_simulator_api("GET", "/list-parking-spots") or []
    for s in spots:
        name = s.get("name", "")
        z = parse_zone(s.get("zone", s.get("Zone", 0)))
        if z == zone and "entry" in name.lower():
            return name
        if normalize_gate_name(name) in [f"entry{zone}", f"entryspot{zone}"]:
            return name
    return ZONE_CONFIG[zone]["entry_spot"]

# ==============================================================================
# SAFE BARRIER OPERATIONS WITH POST-MAINTENANCE VERIFICATION
# ==============================================================================
def repair_and_recover_gate(gate_name, should_open_after=False):
    norm = normalize_gate_name(gate_name)
    b = get_barrier_status(gate_name)
    actual_name = b.get("name", gate_name) if b else gate_name

    if norm in active_gate_repairs:
        return

    # STEP 1: Lock gate immediately
    active_gate_repairs.add(norm)
    gate_wants_maintenance.add(norm)

    if norm in gate_close_timers:
        gate_close_timers[norm].cancel()

    try:
        is_already_broken = False
        if b and (b.get("broken", False) or str(b.get("state", "")).lower() == "broken"):
            is_already_broken = True

        # STEP 2: Physically lower arm if not broken
        if not is_already_broken:
            curr = get_barrier_status(actual_name)
            curr_state = str(curr.get("state", "")).lower() if curr else ""
            if curr_state not in ["closed", "closing"]:
                print(f"[GATE CLOSE] Lowering '{actual_name}' before maintenance...")
                call_simulator_api("POST", f"/barrier-gates/{actual_name}/close")

            for _ in range(16):
                time.sleep(0.25)
                st = get_barrier_status(actual_name)
                if st and str(st.get("state", "")).lower() == "closed":
                    break
        else:
            print(f"[GATE SERVICE] '{actual_name}' is broken. Sending /repair directly...")

        # STEP 3: Issue repair & reset cycle count
        print(f"\n🔧 [GATE MAINTENANCE] Servicing '{actual_name}' (Arm verified DOWN)...")
        call_simulator_api("POST", f"/barrier-gates/{actual_name}/repair")
        reset_component_maintenance(actual_name)

        # STEP 4: Poll until simulator confirms repair is completed
        for _ in range(24):
            time.sleep(0.5)
            status = get_barrier_status(actual_name)
            if status:
                st = str(status.get("state", "")).lower()
                is_maint = (
                    st in ["repairing", "maintenance", "undermaintenance", "broken"] or
                    status.get("broken") or status.get("isUnderMaintenance") or status.get("underMaintenance")
                )
                if not is_maint:
                    print(f"✅ [GATE RESTORED] '{actual_name}' repair complete.")
                    break

        # Settle delay
        time.sleep(0.5)

        if should_open_after:
            time.sleep(0.3)
            call_simulator_api("POST", f"/barrier-gates/{actual_name}/open")

    finally:
        active_gate_repairs.discard(norm)
        gate_wants_maintenance.discard(norm)

        # IMMEDIATE POST-MAINTENANCE ADMISSION CHECK:
        # If a vehicle is waiting at the sensor box, admit it into the zone immediately!
        zone = ZONE_ENTRY_GATES.get(norm)
        if zone:
            e_box = get_entry_spot_exact_name(zone)
            spots = call_simulator_api("GET", "/list-parking-spots") or []
            box_obj = next((s for s in spots if s.get("name") == e_box), None)
            if box_obj and not is_spot_empty(box_obj):
                detected = box_obj.get("detectedCars")
                plate = detected[0] if isinstance(detected, list) and detected else None
                if plate:
                    clean_p = str(plate).strip().replace(" ", "")
                    print(f"🚀 [POST-MAINTENANCE ADMIT] Gate '{actual_name}' restored! Releasing waiting car '{clean_p}' into Zone {zone}...")
                    t_spot, s_zone = allocate_parking_spot("Normal", preferred_zone=zone, allowed_zones=[zone])
                    if t_spot:
                        safe_open_gate(actual_name)
                        dispatch_car_when_gate_open(clean_p, t_spot, actual_name)

def safe_open_gate(gate_name):
    norm = normalize_gate_name(gate_name)
    if norm in active_gate_repairs:
        print(f"[SAFE GATE BLOCKED] Cannot open '{gate_name}': In maintenance!")
        return

    b = get_barrier_status(gate_name)
    if not b: return

    actual_name = b.get("name", gate_name)
    if b.get("broken", False) or b.get("isUnderMaintenance", False) or not is_gate_operational(actual_name):
        threading.Thread(target=repair_and_recover_gate, args=(actual_name, False), daemon=True).start()
        return

    if norm in gate_close_timers:
        gate_close_timers[norm].cancel()

    st = str(b.get("state", "")).lower()
    if st not in ["open", "opening"]:
        call_simulator_api("POST", f"/barrier-gates/{actual_name}/open")
        zone = ZONE_ENTRY_GATES.get(actual_name, ZONE_EXIT_GATES.get(actual_name, 1))
        record_component_cycle(actual_name, "gate", zone)

def safe_close_gate(gate_name):
    b = get_barrier_status(gate_name)
    if not b: return

    actual_name = b.get("name", gate_name)
    if b.get("broken", False) or b.get("isUnderMaintenance", False) or normalize_gate_name(gate_name) in active_gate_repairs:
        return

    st = str(b.get("state", "")).lower()
    if st not in ["closed", "closing", "broken", "repairing"]:
        call_simulator_api("POST", f"/barrier-gates/{actual_name}/close")

def schedule_gate_close(gate_name, delay_seconds=1.5):
    norm = normalize_gate_name(gate_name)
    if norm in gate_close_timers:
        gate_close_timers[norm].cancel()

    def _close_action():
        safe_close_gate(gate_name)
        # Check if gate reached 7 cycles upon closing and trigger maintenance immediately
        b = get_barrier_status(gate_name)
        actual_name = b.get("name", gate_name) if b else gate_name
        cycles = get_component_cycles(actual_name)
        if cycles >= MAINTENANCE_THRESHOLDS["gate_cycles"] and norm not in active_gate_repairs:
            print(f"\n🕒 [POST-ENTRY SERVICE] Gate '{actual_name}' reached {cycles} cycles. Starting maintenance on closed gate...")
            threading.Thread(target=repair_and_recover_gate, args=(actual_name, False), daemon=True).start()

    timer = threading.Timer(delay_seconds, _close_action)
    gate_close_timers[norm] = timer
    timer.start()

def dispatch_car_when_gate_open(plate, spot, gate_name):
    """
    Never aborts and drops the car!
    Waits for the gate to open (up to 4.0s). If gate is opening slowly, re-issues /open.
    Guarantees the car moves into the zone once the arm is raised.
    """
    def _dispatch_worker():
        actual_name = get_barrier_status(gate_name).get("name", gate_name) if get_barrier_status(gate_name) else gate_name

        for i in range(18):
            b = get_barrier_status(gate_name)
            if b and str(b.get("state", "")).lower() == "open":
                break

            # If gate hasn't opened yet after 1.25s, retry open command
            if i == 5:
                call_simulator_api("POST", f"/barrier-gates/{actual_name}/open")

            time.sleep(0.25)

        time.sleep(0.2)
        call_simulator_api("POST", f"/car/{plate}/goto/{spot}")

    threading.Thread(target=_dispatch_worker, daemon=True).start()

def send_car_to_next_entry(plate, next_entry_box):
    def _transit_worker():
        time.sleep(1.0)
        print(f"🛣️ [AVENUE TRANSIT] Routing '{plate}' down avenue to '{next_entry_box}'...")
        call_simulator_api("POST", f"/car/{plate}/goto/{next_entry_box}")
        time.sleep(1.5)
        # Retry command once to guarantee pathfinder doesn't drop the move
        call_simulator_api("POST", f"/car/{plate}/goto/{next_entry_box}")

    threading.Thread(target=_transit_worker, daemon=True).start()

# ==============================================================================
# SAFE SPOT MAINTENANCE
# ==============================================================================
def safe_repair_parking_spot(spot_name):
    if spot_name in pending_spot_repairs:
        return
    pending_spot_repairs.add(spot_name)

    def _worker():
        try:
            with state_lock:
                reserved_spots.add(spot_name)

            print(f"[SPOT SERVICE] Bay '{spot_name}' undergoing maintenance...")
            call_simulator_api("POST", f"/parking-spots/{spot_name}/repair")
            reset_component_maintenance(spot_name)

            for _ in range(16):
                time.sleep(0.5)
                spots = call_simulator_api("GET", "/list-parking-spots") or []
                s_obj = next((s for s in spots if s.get("name") == spot_name), None)
                if s_obj and not s_obj.get("broken", False) and not s_obj.get("isUnderMaintenance", False):
                    print(f"[SPOT SERVICE] Bay '{spot_name}' restored.")
                    break
        finally:
            with state_lock:
                reserved_spots.discard(spot_name)
            pending_spot_repairs.discard(spot_name)

    threading.Thread(target=_worker, daemon=True).start()

# ==============================================================================
# MASTER GATE 7 WATCHDOG & MULTI-STATE SUPERVISOR
# ==============================================================================
def master_gate_watchdog():
    time.sleep(3)
    while True:
        try:
            # 1. Gate 7 Intake
            b7 = get_barrier_status(MAIN_INTAKE_GATE)
            if b7:
                is_broken = b7.get("broken", False)
                is_maint = b7.get("isUnderMaintenance", False)
                state = str(b7.get("state", "")).lower()
                if is_broken and not is_maint:
                    repair_and_recover_gate(MAIN_INTAKE_GATE, should_open_after=True)
                elif state not in ["open", "opening"] and not is_broken and not is_maint:
                    call_simulator_api("POST", f"/barrier-gates/{MAIN_INTAKE_GATE}/open")

            # 2. Self-Healing Entry Supervisor
            spots = call_simulator_api("GET", "/list-parking-spots") or []
            for s in spots:
                name = s.get("name", "")
                if "entry" in name.lower() and not is_spot_empty(s):
                    detected = s.get("detectedCars")
                    plate = detected[0] if isinstance(detected, list) and detected else None
                    if not plate: continue
                    clean_p = str(plate).strip().replace(" ", "")
                    z = parse_zone(s.get("zone", s.get("Zone", 1)))
                    g = ZONE_CONFIG[z]["entry_gate"]

                    # Gate is NOT operational: kick vehicle to next open entry
                    if not is_gate_operational(g):
                        if z == 1:
                            # Try Zone 2 first; if Zone 2 also in maintenance, cascade to Zone 3!
                            if is_gate_operational(ZONE_CONFIG[2]["entry_gate"]):
                                next_box = get_entry_spot_exact_name(2)
                                print(f"🚨 [SUPERVISOR DIVERSION] Diverting '{clean_p}' from {name} -> {next_box} (Zone 2)")
                                send_car_to_next_entry(clean_p, next_box)
                            elif is_gate_operational(ZONE_CONFIG[3]["entry_gate"]):
                                next_box = get_entry_spot_exact_name(3)
                                print(f"🚨 [SUPERVISOR DIVERSION] Both gates down! Diverting '{clean_p}' from {name} -> {next_box} (Zone 3)")
                                send_car_to_next_entry(clean_p, next_box)
                        elif z == 2:
                            if is_gate_operational(ZONE_CONFIG[3]["entry_gate"]):
                                next_box = get_entry_spot_exact_name(3)
                                print(f"🚨 [SUPERVISOR DIVERSION] Diverting '{clean_p}' from {name} -> {next_box} (Zone 3)")
                                send_car_to_next_entry(clean_p, next_box)
                    else:
                        # Gate IS operational, but closed! Raise arm and admit vehicle
                        bg = get_barrier_status(g)
                        if bg and str(bg.get("state", "")).lower() in ["closed", "closing"]:
                            print(f"🚨 [SUPERVISOR UNJAM] Gate '{g}' closed with vehicle '{clean_p}' waiting. Opening...")
                            safe_open_gate(g)
                            t_spot, s_zone = allocate_parking_spot("Normal", preferred_zone=z, allowed_zones=[z])
                            if t_spot:
                                dispatch_car_when_gate_open(clean_p, t_spot, g)

        except Exception as e:
            print(f"[WATCHDOG ERROR] {e}")

        time.sleep(2.0)

# ==============================================================================
# ALLOCATION: STRICT PREFIX-MATCHING & EV/ACCESSIBLE FIRST
# ==============================================================================
def allocate_parking_spot(car_type, preferred_zone=1, allowed_zones=None):
    spots = call_simulator_api("GET", "/list-parking-spots")
    if not isinstance(spots, list): return None, preferred_zone

    c_type = (car_type or "Normal").strip().lower()

    with state_lock:
        usable_spots = [
            s for s in spots
            if s.get("purpose") == "Park"
            and not s.get("broken")
            and not s.get("isUnderMaintenance")
            and is_spot_empty(s)
            and s.get("name") not in reserved_spots
            and (allowed_zones is None or get_spot_zone(s) in allowed_zones)
        ]

        chosen = None

        if c_type in ["electric", "ev"]:
            ev_in_zone = [s for s in usable_spots if str(s.get("parkingForCarType", "")).lower() == "electric" and get_spot_zone(s) == preferred_zone]
            ev_other_zones = [s for s in usable_spots if str(s.get("parkingForCarType", "")).lower() == "electric" and get_spot_zone(s) != preferred_zone]

            if ev_in_zone: chosen = ev_in_zone[0]
            elif ev_other_zones: chosen = ev_other_zones[0]
            else:
                any_in_zone = [s for s in usable_spots if str(s.get("parkingForCarType", "")).lower() == "any" and get_spot_zone(s) == preferred_zone]
                any_other_zones = [s for s in usable_spots if str(s.get("parkingForCarType", "")).lower() == "any" and get_spot_zone(s) != preferred_zone]
                if any_in_zone: chosen = any_in_zone[0]
                elif any_other_zones: chosen = any_other_zones[0]

        elif c_type in ["accessible", "disabled", "handicapped"]:
            acc_in_zone = [s for s in usable_spots if str(s.get("parkingForCarType", "")).lower() == "accessible" and get_spot_zone(s) == preferred_zone]
            acc_other_zones = [s for s in usable_spots if str(s.get("parkingForCarType", "")).lower() == "accessible" and get_spot_zone(s) != preferred_zone]

            if acc_in_zone: chosen = acc_in_zone[0]
            elif acc_other_zones: chosen = acc_other_zones[0]
            else:
                any_in_zone = [s for s in usable_spots if str(s.get("parkingForCarType", "")).lower() == "any" and get_spot_zone(s) == preferred_zone]
                any_other_zones = [s for s in usable_spots if str(s.get("parkingForCarType", "")).lower() == "any" and get_spot_zone(s) != preferred_zone]
                if any_in_zone: chosen = any_in_zone[0]
                elif any_other_zones: chosen = any_other_zones[0]

        else:
            any_in_zone = [s for s in usable_spots if str(s.get("parkingForCarType", "")).lower() == "any" and get_spot_zone(s) == preferred_zone]
            any_other_zones = [s for s in usable_spots if str(s.get("parkingForCarType", "")).lower() == "any" and get_spot_zone(s) != preferred_zone]
            if any_in_zone: chosen = any_in_zone[0]
            elif any_other_zones: chosen = any_other_zones[0]

        if chosen:
            spot_name = chosen.get("name")
            spot_zone = get_spot_zone(chosen)
            reserved_spots.add(spot_name)
            record_component_cycle(spot_name, "spot", spot_zone)
            return spot_name, spot_zone

    return None, preferred_zone

# ==============================================================================
# LIGHTING & EXHAUST AUTOMATION
# ==============================================================================
def sync_lights():
    global hardware_inventory, is_night_time
    now = time.time()

    for light_name, meta in list(hardware_inventory["lights"].items()):
        zone = meta.get("zone", 1)
        cars_moving = len(moving_cars_per_zone.get(zone, set())) > 0
        should_be_on = is_night_time and cars_moving

        current_state = meta.get("state", False)
        if should_be_on and not current_state:
            call_simulator_api("POST", f"/lights/{light_name}/on")
            meta["state"] = True
            meta["last_on"] = now
        elif not should_be_on and current_state:
            call_simulator_api("POST", f"/lights/{light_name}/off")
            meta["state"] = False
            elapsed = now - meta.get("last_on", now)
            record_component_runtime(light_name, "light", zone, elapsed)

def sync_exhaust_fans(zone=None):
    global hardware_inventory, co_levels_per_zone
    now = time.time()
    zones_to_check = [zone] if zone else [1, 2, 3]

    for z in zones_to_check:
        co = co_levels_per_zone.get(z, 0.0)
        should_run = (co >= 50.0)

        for fan_name, meta in list(hardware_inventory["fans"].items()):
            if meta.get("zone") == z:
                current_state = meta.get("state", False)
                if should_run and not current_state:
                    call_simulator_api("POST", f"/exhaust-fans/{fan_name}/on")
                    meta["state"] = True
                    meta["last_on"] = now
                elif not should_run and current_state:
                    call_simulator_api("POST", f"/exhaust-fans/{fan_name}/off")
                    meta["state"] = False
                    elapsed = now - meta.get("last_on", now)
                    record_component_runtime(fan_name, "fan", z, elapsed)

# ==============================================================================
# PREVENTIVE MAINTENANCE SCHEDULER
# ==============================================================================
def preventive_maintenance_loop():
    time.sleep(6)
    print(f"[PREVENTIVE MAINTENANCE] Scheduler active (Gate threshold: {MAINTENANCE_THRESHOLDS['gate_cycles']} cycles)...")

    while True:
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute("SELECT component_name, component_type, zone, usage_cycles, run_seconds FROM component_usage")
            records = c.fetchall()
            conn.close()

            spots_live = call_simulator_api("GET", "/list-parking-spots") or []

            for name, comp_type, zone, cycles, runtime in records:
                # 1. Scheduled Gate Maintenance: Proactively mark gate when threshold is reached
                if comp_type == "gate" and cycles >= MAINTENANCE_THRESHOLDS["gate_cycles"]:
                    norm = normalize_gate_name(name)
                    gate_wants_maintenance.add(norm)

                    if norm not in active_gate_repairs and len(active_gate_repairs) == 0:
                        entry_box = ZONE_CONFIG.get(zone, {}).get("entry_spot", "")
                        box_obj = next((s for s in spots_live if s.get("name") == entry_box), None)
                        is_box_clear = is_spot_empty(box_obj) if box_obj else True

                        if is_box_clear:
                            print(f"\n🕒 [PREVENTIVE TRIGGER] Gate '{name}' reached {cycles} cycles. Servicing...")
                            is_main = (norm == normalize_gate_name(MAIN_INTAKE_GATE))
                            threading.Thread(target=repair_and_recover_gate, args=(name, is_main), daemon=True).start()

                # 2. Scheduled Spot Maintenance
                elif comp_type == "spot" and cycles >= MAINTENANCE_THRESHOLDS["spot_cycles"]:
                    spot_obj = next((s for s in spots_live if s.get("name") == name), None)
                    if spot_obj and is_spot_empty(spot_obj) and name not in reserved_spots:
                        safe_repair_parking_spot(name)

                # 3. Scheduled Fan Maintenance
                elif comp_type == "fan" and runtime >= MAINTENANCE_THRESHOLDS["fan_run_seconds"]:
                    meta = hardware_inventory["fans"].get(name, {})
                    if not meta.get("state", False) and co_levels_per_zone.get(zone, 0) < 50:
                        call_simulator_api("POST", f"/exhaust-fans/{name}/repair")
                        reset_component_maintenance(name)

                # 4. Scheduled Light Maintenance
                elif comp_type == "light" and runtime >= MAINTENANCE_THRESHOLDS["light_run_seconds"]:
                    meta = hardware_inventory["lights"].get(name, {})
                    if not meta.get("state", False):
                        call_simulator_api("POST", f"/lights/{name}/repair")
                        reset_component_maintenance(name)

        except Exception as e:
            print(f"[MAINTENANCE LOOP ERROR] {e}")

        time.sleep(3)

# ==============================================================================
# STARTUP INITIALIZATION
# ==============================================================================
def initialize_system():
    global MAIN_INTAKE_GATE
    time.sleep(2)
    print("\n--- [SYSTEM STARTUP & INITIAL SYNC] ---")
    if not login_to_simulator(): return

    # Clean database counters from previous sessions
    reset_all_session_cycles()

    barriers = call_simulator_api("GET", "/list-barriers") or []
    gate7_is_broken = False
    for b in barriers:
        b_name = b.get("name", "")
        if "7" in b_name: 
            MAIN_INTAKE_GATE = b_name
            if b.get("broken", False):
                gate7_is_broken = True

    for g in ZONE_ENTRY_GATES.keys(): safe_close_gate(g)
    for g in ZONE_EXIT_GATES.keys(): safe_close_gate(g)

    if gate7_is_broken:
        print(f"[STARTUP] Gate 7 is broken at start! Repairing before opening...")
        call_simulator_api("POST", f"/barrier-gates/{MAIN_INTAKE_GATE}/repair")
        for _ in range(16):
            time.sleep(0.5)
            st = get_barrier_status(MAIN_INTAKE_GATE)
            if st and not st.get("broken", False) and not st.get("isUnderMaintenance", False):
                print("[STARTUP] Gate 7 successfully repaired.")
                break
        safe_open_gate(MAIN_INTAKE_GATE)
    else:
        safe_open_gate(MAIN_INTAKE_GATE)

    fans = call_simulator_api("GET", "/list-exhaust-fans") or []
    for f in fans:
        name = f.get("name")
        z = parse_zone(f.get("zone", f.get("Zone", 1)))
        hardware_inventory["fans"][name] = {"zone": z, "state": False, "last_on": 0.0}
        call_simulator_api("POST", f"/exhaust-fans/{name}/off")

    lights = call_simulator_api("GET", "/list-lights") or []
    for l in lights:
        name = l.get("name")
        z = parse_zone(l.get("zone", l.get("Zone", 1)))
        hardware_inventory["lights"][name] = {"zone": z, "state": False, "last_on": 0.0}
        call_simulator_api("POST", f"/lights/{name}/off")

    spots = call_simulator_api("GET", "/list-parking-spots") or []
    with state_lock:
        for s in spots:
            if s.get("purpose") == "Park" and not is_spot_empty(s):
                reserved_spots.add(s.get("name"))

    print("--- [STARTUP SYNC COMPLETE] ---\n")

# ==============================================================================
# WEBHOOK EVENT HANDLER
# ==============================================================================
def resolve_entry_zone(spot_name, zone_hint=1):
    clean = normalize_gate_name(spot_name)
    if "entry1" in clean or clean == "gate1": return 1
    if "entry2" in clean or clean == "gate3": return 2
    if "entry3" in clean or clean == "gate5": return 3
    return parse_zone(zone_hint)

@app.route("/webhook", methods=["GET", "POST"])
def webhook_listener():
    global is_night_time
    if request.method == "GET":
        return jsonify({"status": "online"}), 200

    data = request.get_json(force=True, silent=True) or {}
    event_id = data.get("EventId")
    event_class = data.get("EventClass")

    if event_id:
        if event_id in processed_event_ids:
            return jsonify({"status": "duplicate"}), 200
        processed_event_ids.add(event_id)

    # Simulator Day / Night
    if "IsNight" in data or "isNight" in data:
        is_night_time = bool(data.get("IsNight", data.get("isNight")))
        sync_lights()
    elif "TimeOfDay" in data:
        tod = str(data.get("TimeOfDay")).lower()
        is_night_time = any(w in tod for w in ["night", "evening", "dark"])
        sync_lights()

    # 1. Car Movement Events
    if event_class == "car_spot_action":
        car_plate = data.get("CarPlateNumber")
        spot_name = str(data.get("SpotName", ""))
        spot_type = str(data.get("SpotType", ""))
        direction = str(data.get("Direction", ""))
        car_type = data.get("CarType", "Normal")
        planned_duration = int(data.get("PlannedParkingDurationInMinutes", 0))

        if not car_plate:
            return jsonify({"status": "ignored"}), 200

        raw_plate = car_plate.strip()
        nospace_plate = raw_plate.replace(" ", "")

        # Master Gate 7 intake
        if normalize_gate_name(spot_name) == normalize_gate_name(MAIN_INTAKE_GATE):
            if direction == "CarIn":
                safe_open_gate(MAIN_INTAKE_GATE)
            return jsonify({"status": "ok"}), 200

        is_entry_sensor = (
            spot_type in ["EntrySpot", "EntryGate"] or 
            any(k in normalize_gate_name(spot_name) for k in ["entry1", "entry2", "entry3", "gate1", "gate3", "gate5"])
        )

        # ----------------------------------------------------------------------
        # A. CAR ARRIVES AT AN ENTRY BOX (direction == "CarIn")
        # ----------------------------------------------------------------------
        if is_entry_sensor and direction == "CarIn":
            arrival_zone = resolve_entry_zone(spot_name, data.get("Zone", 1))
            entry_gate = ZONE_CONFIG[arrival_zone]["entry_gate"]
            in_transit_entries.discard(ZONE_CONFIG[arrival_zone]["entry_spot"])

            # ------------------------------------------------------------------
            # GATE IN SERVICE OR THRESHOLD REACHED: CASCADING DIVERSION
            # ------------------------------------------------------------------
            if not is_gate_operational(entry_gate):
                print(f"\n⚠️ [GATE UNAVAILABLE] '{entry_gate}' at '{spot_name}' reached cycle limit or servicing. Rerouting...")
                safe_close_gate(entry_gate)

                chosen_entry_box = None
                chosen_zone = None

                # RULE 1: ZONE 1 DIVERTS TO ZONE 2 (ENTRY2); IF ZONE 2 ALSO DOWN, CASCADE TO ZONE 3!
                if arrival_zone == 1:
                    if is_gate_operational(ZONE_CONFIG[2]["entry_gate"]):
                        chosen_entry_box = get_entry_spot_exact_name(2)
                        chosen_zone = 2
                    elif is_gate_operational(ZONE_CONFIG[3]["entry_gate"]):
                        chosen_entry_box = get_entry_spot_exact_name(3)
                        chosen_zone = 3

                # RULE 2: ZONE 2 DIVERTS TO ZONE 3 (ENTRY3)
                elif arrival_zone == 2:
                    if is_gate_operational(ZONE_CONFIG[3]["entry_gate"]):
                        chosen_entry_box = get_entry_spot_exact_name(3)
                        chosen_zone = 3

                if chosen_entry_box:
                    in_transit_entries.add(chosen_entry_box)
                    print(f"🔄 [DIVERSION ROUTE] Car '{raw_plate}' routed from {spot_name} -> '{chosen_entry_box}' (Zone {chosen_zone}).")
                    send_car_to_next_entry(nospace_plate, chosen_entry_box)
                    return jsonify({"status": "diverted_to_entry", "next_entry": chosen_entry_box}), 200
                else:
                    print(f"[CONGESTION] Target entry for '{raw_plate}' busy. Waiting briefly at {spot_name}...")
                    return jsonify({"status": "waiting"}), 200

            # ------------------------------------------------------------------
            # GATE OPERATIONAL: NORMAL ADMISSION
            # ------------------------------------------------------------------
            target_spot, spot_zone = allocate_parking_spot(car_type, preferred_zone=arrival_zone, allowed_zones=[arrival_zone])
            if not target_spot:
                other_zones = [z for z in [1, 2, 3] if z != arrival_zone and is_gate_operational(ZONE_CONFIG[z]["entry_gate"])]
                target_spot, spot_zone = allocate_parking_spot(car_type, preferred_zone=arrival_zone, allowed_zones=other_zones)

            target_gate = ZONE_CONFIG[spot_zone]["entry_gate"]
            if target_spot:
                active_cars[raw_plate] = {
                    "spot": target_spot,
                    "type": car_type,
                    "zone": spot_zone,
                    "entry_gate": target_gate,
                    "duration": max(1, planned_duration),
                    "charged": False,
                    "parked": False
                }
                log_car_entry(raw_plate, car_type, target_spot, spot_zone)

                moving_cars_per_zone[arrival_zone].add(raw_plate)
                sync_lights()

                safe_open_gate(target_gate)
                dispatch_car_when_gate_open(nospace_plate, target_spot, target_gate)
            else:
                print(f"[LOT FULL] No matching parking spot for '{raw_plate}'.")

        # ----------------------------------------------------------------------
        # B. CAR CLEARS SENSOR BOX (direction == "CarOut") -> LOWER IN 1.5s
        # ----------------------------------------------------------------------
        elif is_entry_sensor and direction == "CarOut":
            zone = resolve_entry_zone(spot_name, data.get("Zone", 1))
            entry_gate = ZONE_CONFIG[zone]["entry_gate"]
            in_transit_entries.discard(ZONE_CONFIG[zone]["entry_spot"])
            # 1.5s delay lowers arm right behind the car to prevent tailgating
            schedule_gate_close(entry_gate, delay_seconds=1.5)

        # ----------------------------------------------------------------------
        # C1. CAR PHYSICALLY ENTERS PARKING BAY
        # ----------------------------------------------------------------------
        elif spot_type == "Park" and direction == "CarIn":
            car_info = active_cars.setdefault(raw_plate, {})
            zone = car_info.get("zone", 1)
            car_info["parked"] = True

            moving_cars_per_zone[zone].discard(raw_plate)
            sync_lights()

            entry_gate = car_info.get("entry_gate")
            if entry_gate:
                safe_close_gate(entry_gate)

            if planned_duration > 0:
                car_info["duration"] = planned_duration

        # ----------------------------------------------------------------------
        # C2. CAR LEAVES BAY -> ROUTE TO EXACT ZONE EXIT GATE
        # ----------------------------------------------------------------------
        elif spot_type == "Park" and direction == "CarOut":
            car_info = active_cars.get(raw_plate, {})
            car_info["parked"] = False
            zone = get_spot_zone({"name": spot_name})

            moving_cars_per_zone[zone].add(raw_plate)
            sync_lights()

            exit_gate = ZONE_CONFIG[zone]["exit_gate"]
            print(f"[EXIT DISPATCH] '{raw_plate}' left bay '{spot_name}' (Zone {zone}). Routing to {exit_gate}...")
            call_simulator_api("POST", f"/car/{nospace_plate}/goto/{exit_gate}")

            def _post_park_cleanup(s_name):
                time.sleep(2.5)
                with state_lock:
                    reserved_spots.discard(s_name)

                cycles = get_component_cycles(s_name)
                if cycles >= MAINTENANCE_THRESHOLDS["spot_cycles"]:
                    safe_repair_parking_spot(s_name)

            threading.Thread(target=_post_park_cleanup, args=(spot_name,), daemon=True).start()

        # ----------------------------------------------------------------------
        # D. CAR ARRIVES AT EXIT GATE
        # ----------------------------------------------------------------------
        elif (spot_type in ["ExitSpot", "ExitGate"] or spot_name in ZONE_EXIT_GATES) and direction == "CarIn":
            zone = ZONE_EXIT_GATES.get(spot_name, parse_zone(data.get("Zone", 1)))
            exit_gate = ZONE_CONFIG[zone]["exit_gate"]
            safe_close_gate(exit_gate)

            car_info = active_cars.get(raw_plate, {})
            duration = max(1, int(car_info.get("duration", 1)))
            is_electric = (str(car_info.get("type", "")).lower() == "electric")

            parking_cost = float(duration)
            charging_cost = float(duration * 2) if is_electric else 0.0
            total_fee = parking_cost + charging_cost

            car_info["parking_cost"] = parking_cost
            car_info["charging_cost"] = charging_cost
            car_info["expected_cost"] = total_fee

            log_car_exit(raw_plate, parking_cost, charging_cost, total_fee)

            if not car_info.get("charged"):
                car_info["charged"] = True
                def _charge_car(plate, p_cost, c_cost):
                    time.sleep(1.5)
                    charge_params = {"parkingCost": p_cost, "chargingCost": c_cost}
                    call_simulator_api("POST", f"/car/{plate}/charge", payload=charge_params, params=charge_params)

                threading.Thread(target=_charge_car, args=(nospace_plate, parking_cost, charging_cost), daemon=True).start()

        # ----------------------------------------------------------------------
        # E. CAR CLEARED EXIT GATE
        # ----------------------------------------------------------------------
        elif (spot_type in ["ExitSpot", "ExitGate"] or spot_name in ZONE_EXIT_GATES) and direction == "CarOut":
            zone = ZONE_EXIT_GATES.get(spot_name, parse_zone(data.get("Zone", 1)))
            exit_gate = ZONE_CONFIG[zone]["exit_gate"]
            schedule_gate_close(exit_gate, delay_seconds=2.0)

            moving_cars_per_zone[zone].discard(raw_plate)
            sync_lights()
            active_cars.pop(raw_plate, None)

    # 2. Payment Confirmed -> Raise Exit Gate
    elif event_class == "payment_made":
        car_plate = data.get("CarPlateNumber", "").strip()
        nospace_plate = car_plate.replace(" ", "")
        car_info = active_cars.get(car_plate, {})
        zone = car_info.get("zone", 1)
        exit_gate = ZONE_CONFIG[zone]["exit_gate"]

        raw_amount = data.get("Amount") or data.get("amount")
        amount = float(raw_amount) if raw_amount is not None else float(car_info.get("expected_cost", 1.0))
        p_cost = float(car_info.get("parking_cost", 1.0))
        c_cost = float(car_info.get("charging_cost", 0.0))
        log_car_exit(car_plate, p_cost, c_cost, amount)

        safe_open_gate(exit_gate)

        def _dispatch_exit(plate, g_name):
            time.sleep(1.5)
            call_simulator_api("POST", f"/car/{plate}/goto/leavepark")
            schedule_gate_close(g_name, delay_seconds=3.0)

        threading.Thread(target=_dispatch_exit, args=(nospace_plate, exit_gate), daemon=True).start()

    # 3. Random Component Breakdowns (REACTIVE REPAIR PRESERVED)
    elif event_class == "component_broken":
        c_type = data.get("Type")
        c_name = data.get("Name")
        print(f"\n⚡ [RANDOM BREAKDOWN DETECTED] {c_type} '{c_name}' failed. Repairing...")

        if c_type == "BarrierGate":
            is_main = (normalize_gate_name(c_name) == normalize_gate_name(MAIN_INTAKE_GATE))
            threading.Thread(target=repair_and_recover_gate, args=(c_name, is_main), daemon=True).start()
        elif c_type in ["Parking", "ParkingSpot"]:
            spots = call_simulator_api("GET", "/list-parking-spots") or []
            s_obj = next((s for s in spots if s.get("name") == c_name), None)
            if s_obj and is_spot_empty(s_obj):
                safe_repair_parking_spot(c_name)
        elif c_type == "ExhaustFan":
            call_simulator_api("POST", f"/exhaust-fans/{c_name}/repair")
            reset_component_maintenance(c_name)
        elif c_type == "Light":
            call_simulator_api("POST", f"/lights/{c_name}/repair")
            reset_component_maintenance(c_name)

    # 4. Carbon Monoxide Sensor (CO < 50 Rule)
    elif event_class == "carbon_monoxide_event":
        co_val = float(data.get("COLevel", data.get("CoLevel", data.get("Value", 0.0))))
        danger = data.get("DangerLevel", "Safe")
        zone = parse_zone(data.get("Zone", 1))

        if danger in ["Mid", "High", "Critical"] and co_val == 0.0:
            co_val = 65.0
        elif danger == "Safe" and co_val == 0.0:
            co_val = 15.0

        co_levels_per_zone[zone] = co_val
        sync_exhaust_fans(zone)

    # 5. Penalties Logged
    elif event_class == "penalty":
        reason = data.get("Reason", "Unknown")
        fine = float(data.get("FineAmount", 0.0))
        log_penalty(reason, fine)
        print(f"\n🚨 [PENALTY INCURRED] {reason} | -{fine} credits 🚨\n")

    return jsonify({"status": "ok"}), 200

# ==============================================================================
# DASHBOARD ROUTES & CONTROLS
# ==============================================================================
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "username" not in session or session.get("role") != "admin":
            return "Access Denied: Admins Only", 403
        return f(*args, **kwargs)
    return decorated_function

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        action = request.form.get("action") 
        username = request.form.get("username").strip()
        password = request.form.get("password")
        users = load_users()
        
        if action == "login":
            user = users.get(username)
            if user and user["password"] == password:
                session["username"] = username
                session["role"] = user["role"]
                return redirect(url_for("admin_dashboard" if user["role"] == "admin" else "operator_dashboard"))
            else:
                error = "Invalid credentials."
        elif action == "signup":
            role = request.form.get("role")
            if username in users:
                error = "Username already exists!"
            else:
                users[username] = {"password": password, "role": role}
                save_users(users)
                session["username"] = username
                session["role"] = role
                return redirect(url_for("admin_dashboard" if role == "admin" else "operator_dashboard"))
            
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def root():
    return redirect(url_for("admin_dashboard" if session.get("role") == "admin" else "operator_dashboard"))

@app.route("/operator", methods=["GET"])
@login_required
def operator_dashboard():
    return render_template("operator.html", username=session.get("username"))

@app.route("/admin", methods=["GET"])
@admin_required
def admin_dashboard():
    return render_template("admin.html", username=session.get("username"))

@app.route("/api/operator/gate/<name>/<action>", methods=["POST"])
@login_required
def operator_gate(name, action):
    if action == "repair":
        is_main = (normalize_gate_name(name) == normalize_gate_name(MAIN_INTAKE_GATE))
        threading.Thread(target=repair_and_recover_gate, args=(name, is_main), daemon=True).start()
    elif action == "open":
        safe_open_gate(name)
    elif action == "close":
        safe_close_gate(name)
    return jsonify({"status": "ok"})

@app.route("/api/dashboard/status", methods=["GET"])
@login_required
def dashboard_status():
    spots = call_simulator_api("GET", "/list-parking-spots") or []
    barriers = call_simulator_api("GET", "/list-barriers") or []

    with state_lock:
        free_spots = sum(1 for s in spots if s.get("purpose") == "Park" and is_spot_empty(s) and not s.get("broken") and s.get("name") not in reserved_spots)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT * FROM car_logs ORDER BY id DESC LIMIT 50")
    logs = c.fetchall()
    c.execute("SELECT COUNT(*), COALESCE(SUM(fine_amount), 0) FROM penalty_logs")
    p_count, p_total = c.fetchone()
    c.execute("SELECT COALESCE(SUM(total_paid), 0) FROM car_logs WHERE status = 'Completed'")
    total_revenue = c.fetchone()[0]
    
    c.execute("SELECT component_name, component_type, zone, usage_cycles, run_seconds, last_maintenance FROM component_usage")
    usage_data = c.fetchall()
    conn.close()

    return jsonify({
        "free_spots": free_spots,
        "master_gate": MAIN_INTAKE_GATE,
        "barriers": barriers,
        "spots": spots,
        "co_levels": co_levels_per_zone,
        "is_night": is_night_time,
        "gates_in_maintenance": list(active_gate_repairs),
        "component_usage": usage_data,
        "logs": logs,
        "penalties_count": p_count,
        "penalties_total": p_total,
        "total_revenue": total_revenue
    })

# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================
# ==============================================================================
# MAIN ENTRYPOINT & STARTUP SEQUENCE
# ==============================================================================
if __name__ == "__main__":
    # Step 1: Initialize Database Tables (car_logs, penalty_logs, maintenance_logs, audit_logs)
    database.init_db()

    # Step 2: Live Hardware Synchronization (JWT Login, Gate 7 Check, Initial Spot Sync)
    simulator.initialize_system()

    # Step 3: Start Background Automation Threads
    simulator.start_co_polling()              # Monitors CO gas levels continuously
    simulator.start_preventive_maintenance()  # Level 2 auto-maintenance scheduler
    simulator.start_master_gate_watchdog()    # Gate 7 and unjam supervisor

    # Step 4: Set Initial Environmental Baseline
    for zone in getattr(config, "STATIC_ZONE_FANS", {}):
        simulator.handle_carbon_monoxide_event("Safe", 0, zone)

    print("=" * 70)
    print("  CAR PARK MANAGEMENT SYSTEM (CTRL ALT EVERYTHING)")
    print("  Status: All Background Supervisors & API Endpoints Active")
    print("  Dashboard UI: http://127.0.0.1:5000")
    print("  Webhook Receiver: http://127.0.0.1:5000/webhook")
    print("=" * 70)

    # Step 5: Start Flask Web Server
    app.run(host="0.0.0.0", port=5000, debug=False)

import time
import threading
import hashlib
import urllib.parse
import sqlite3
import re
import requests
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
import json
import os

app = Flask(__name__)
# secret_key
app.secret_key = "ctrl_alt_everything_super_secret_key" 

USERS_FILE = "users.json"

def load_users():
    """从 JSON 文件加载用户，如果文件不存在则自动创建默认的 admin 和 operator"""
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
    """将新用户保存到 JSON 文件"""
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=4)

# ==============================================================================
# CONFIGURATION
# ==============================================================================
SIMULATOR_BASE_URL = "http://127.0.0.1:9898/api/v1"
ADMIN_NAME = "admin"
ADMIN_PASS = "admin"
DB_FILE = "parking.db"

jwt_token = None
auth_lock = threading.Lock()
state_lock = threading.Lock()

# State Tracking
processed_event_ids = set()
active_cars = {}          # { plate: { "spot": str, "type": str, "duration": int, "expected_cost": float, "charged": bool } }
reserved_spots = set()    # Bays reserved by cars driving to them

entry_gate_name = "gateA"
exit_gate_name = "gateB"
exhaust_fans = []

# ==============================================================================
# USER AUTHENTICATION & ROLES
# ==============================================================================
SYSTEM_USERS = {
    "admin": {"password": "admin", "role": "admin"},
    "operator": {"password": "operator", "role": "operator"}
}

def login_required(f):
    """装饰器：检查用户是否已登录"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    """装饰器：检查用户是否为 Admin"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "username" not in session or session.get("role") != "admin":
            return "Access Denied: Admins Only", 403
        return f(*args, **kwargs)
    return decorated_function


# ==============================================================================
# DATABASE SETUP (SQLite)
# ==============================================================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS car_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plate TEXT, car_type TEXT, spot_name TEXT,
        entry_time TEXT, exit_time TEXT,
        parking_cost REAL, charging_cost REAL, total_paid REAL, status TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS penalty_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reason TEXT, fine_amount REAL, timestamp TEXT
    )''')
    # Clean up past unclosed rows so dashboard looks clean immediately
    c.execute('''UPDATE car_logs 
                 SET exit_time = datetime('now', 'localtime'),
                     parking_cost = 1.0, charging_cost = 0.0, total_paid = 1.0, status = 'Completed'
                 WHERE exit_time IS NULL AND status = 'Completed' ''')
    conn.commit()
    conn.close()

def log_car_entry(plate, car_type, spot):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO car_logs (plate, car_type, spot_name, entry_time, status)
                 VALUES (?, ?, ?, datetime('now', 'localtime'), 'Parked')''',
              (plate.strip(), car_type, spot))
    conn.commit()
    conn.close()

def log_car_exit(plate, p_cost, c_cost, total):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''UPDATE car_logs 
                 SET exit_time = datetime('now', 'localtime'),
                     parking_cost = ?, charging_cost = ?, total_paid = ?, status = 'Completed'
                 WHERE id = (
                     SELECT id FROM car_logs 
                     WHERE TRIM(UPPER(plate)) = TRIM(UPPER(?)) 
                     ORDER BY id DESC LIMIT 1
                 )''',
              (p_cost, c_cost, total, plate.strip()))
    conn.commit()
    conn.close()
    print(f"[DB LOGGED] Exit time & fee saved for '{plate}': ${total:.2f}")

def log_penalty(reason, fine):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO penalty_logs (reason, fine_amount, timestamp)
                 VALUES (?, ?, datetime('now', 'localtime'))''',
              (reason, fine))
    conn.commit()
    conn.close()


# ==============================================================================
# SAFE SPOT OCCUPANCY CHECK
# ==============================================================================
def is_spot_empty(spot):
    detected = spot.get("detectedCars")
    if detected is None: return True
    if isinstance(detected, int): return detected == 0
    if isinstance(detected, (list, tuple)): return len(detected) == 0
    return False


# ==============================================================================
# SIMULATOR API CLIENT (JWT AUTHENTICATED)
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
                print(f"[AUTH] Successfully acquired JWT token.")
                return True
            print(f"[AUTH FAILED] Status {res.status_code}: {res.text}")
        except Exception as e:
            print(f"[AUTH ERROR] Cannot connect to simulator on port 9898: {e}")
        return False

def call_simulator_api(method, endpoint, payload=None, params=None):
    global jwt_token
    if not jwt_token and not login_to_simulator():
        return None

    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json"
    }
    url = f"{SIMULATOR_BASE_URL}{endpoint}"

    try:
        if method.upper() == "GET":
            res = requests.get(url, headers=headers, params=params, timeout=5)
        else:
            res = requests.post(url, headers=headers, json=payload or {}, params=params, timeout=5)

        if res.status_code == 401:
            print("[AUTH] Token expired. Re-authenticating...")
            if login_to_simulator():
                headers["Authorization"] = f"Bearer {jwt_token}"
                res = requests.request(method, url, headers=headers, json=payload, params=params, timeout=5)

        return res.json() if res.content and res.status_code in [200, 201] else {}
    except Exception as e:
        print(f"[API ERROR] Request failed for {endpoint}: {e}")
        return None


# ==============================================================================
# SMART SELF-REPAIRING GATE CONTROLLER (WITH TIMED DELAYS)
# ==============================================================================
def safe_open_gate(gate_name):
    barriers = call_simulator_api("GET", "/list-barriers")
    if isinstance(barriers, list):
        for b in barriers:
            if b.get("name") == gate_name and b.get("broken", False):
                print(f"[MAINTENANCE] Gate '{gate_name}' worn out! Auto-repairing...")
                call_simulator_api("POST", f"/barrier-gates/{gate_name}/repair")
                time.sleep(1.2)
                break
    print(f"[GATE] Lifting barrier '{gate_name}'...")
    call_simulator_api("POST", f"/barrier-gates/{gate_name}/open")

def safe_close_gate(gate_name):
    print(f"[GATE] Lowering barrier '{gate_name}'...")
    call_simulator_api("POST", f"/barrier-gates/{gate_name}/close")

def delayed_close_gate(gate_name, delay_seconds=3.0):
    def _close():
        time.sleep(delay_seconds)
        safe_close_gate(gate_name)
    threading.Thread(target=_close, daemon=True).start()


# ==============================================================================
# STRICT LIVE SPOT ALLOCATION & IN-TRANSIT RESERVATION LOCK
# ==============================================================================
def allocate_parking_spot(car_type):
    spots = call_simulator_api("GET", "/list-parking-spots")
    if not isinstance(spots, list): return None

    with state_lock:
        usable_spots = [
            s for s in spots
            if s.get("purpose") == "Park"
            and not s.get("broken")
            and not s.get("isUnderMaintenance")
            and is_spot_empty(s)
            and s.get("name") not in reserved_spots
        ]

        c_type = (car_type or "Normal").strip().capitalize()
        chosen_spot = None

        if c_type == "Electric":
            for s in usable_spots:
                if s.get("parkingForCarType", "").lower() == "electric":
                    chosen_spot = s.get("name")
                    break
            if not chosen_spot:
                for s in usable_spots:
                    if s.get("parkingForCarType", "").lower() == "any":
                        chosen_spot = s.get("name")
                        break
        elif c_type in ["Accessible", "Disabled", "Handicapped"]:
            for s in usable_spots:
                if s.get("parkingForCarType", "").lower() == "accessible":
                    chosen_spot = s.get("name")
                    break
        else:
            for s in usable_spots:
                if s.get("parkingForCarType", "").lower() == "any":
                    chosen_spot = s.get("name")
                    break

        if chosen_spot:
            reserved_spots.add(chosen_spot)
            print(f"[RESERVATION] Bay '{chosen_spot}' reserved for incoming {c_type} car.")
            return chosen_spot

    print(f"[ALLOCATION WARNING] No matching bay available for car type '{c_type}'!")
    return None


# ==============================================================================
# STARTUP HARDWARE SYNC & UNJAM ROUTINE
# ==============================================================================
def initialize_system():
    time.sleep(2)
    print("\n--- [SYSTEM STARTUP: LIVE HARDWARE SYNC] ---")
    if not login_to_simulator(): return

    call_simulator_api("GET", "/test")

    global exhaust_fans
    fans = call_simulator_api("GET", "/list-exhaust-fans")
    if isinstance(fans, list):
        exhaust_fans = [f.get("name") for f in fans if "name" in f]

    # Repair broken gates on startup
    barriers = call_simulator_api("GET", "/list-barriers")
    if isinstance(barriers, list):
        for b in barriers:
            if b.get("broken", False):
                call_simulator_api("POST", f"/barrier-gates/{b.get('name')}/repair")

    # Ensure exit gate is closed by default
    safe_close_gate(exit_gate_name)

    # Sync already parked cars into reserved_spots
    spots = call_simulator_api("GET", "/list-parking-spots")
    if isinstance(spots, list):
        with state_lock:
            for s in spots:
                if s.get("purpose") == "Park" and not is_spot_empty(s):
                    reserved_spots.add(s.get("name"))
                    print(f"[INIT SYNC] Bay '{s.get('name')}' is already occupied.")

    # RESCUE: Check if cars are waiting at Entrance or Exit on boot
    if isinstance(spots, list):
        for s in spots:
            purpose = s.get("purpose")
            detected = s.get("detectedCars")
            has_car = (isinstance(detected, int) and detected > 0) or (isinstance(detected, list) and len(detected) > 0)
            if purpose == "EntrySpot" and has_car:
                print("[INIT RESCUE] Car detected at entrance! Lifting gateA...")
                safe_open_gate(entry_gate_name)
            elif purpose == "ExitSpot" and has_car:
                print("[INIT RESCUE] Car detected at exit! Lifting gateB and releasing...")
                safe_open_gate(exit_gate_name)
                time.sleep(1.5)
                call_simulator_api("POST", "/car/WCT%20759/goto/leavepark")

    print("--- [INITIALIZATION COMPLETE] ---\n")


# ==============================================================================
# WEBHOOK EVENT HANDLER (NO AUTH REQUIRED FOR SIMULATOR)
# ==============================================================================
@app.route("/webhook", methods=["GET", "POST"])
def webhook_listener():
    if request.method == "GET":
        return jsonify({"status": "online"}), 200

    data = request.get_json(force=True, silent=True) or {}
    event_id = data.get("EventId")
    event_class = data.get("EventClass")

    if event_id:
        if event_id in processed_event_ids:
            return jsonify({"status": "duplicate"}), 200
        processed_event_ids.add(event_id)

    # 1. Car movement events
    if event_class == "car_spot_action":
        car_plate = data.get("CarPlateNumber")
        spot_name = data.get("SpotName")
        spot_type = data.get("SpotType")
        direction = data.get("Direction")
        car_type = data.get("CarType", "Normal")
        duration = int(data.get("PlannedParkingDurationInMinutes", 1))

        if not car_plate:
            return jsonify({"status": "ignored"}), 200

        safe_plate = urllib.parse.quote(car_plate)

        # A. Entrance Arrival
        if spot_type == "EntrySpot" and direction == "CarIn":
            target_spot = allocate_parking_spot(car_type)
            if target_spot:
                active_cars[car_plate] = {
                    "spot": target_spot, "type": car_type,
                    "duration": duration, "charged": False
                }
                log_car_entry(car_plate, car_type, target_spot)
                print(f"\n[ENTRY] Admitting '{car_plate}' ({car_type}) -> Bay '{target_spot}'")

                safe_open_gate(entry_gate_name)

                def _dispatch_entry(plate, spot):
                    time.sleep(1.5)
                    print(f"[DISPATCH] Arm raised. Directing '{plate}' into '{spot}'...")
                    call_simulator_api("POST", f"/car/{plate}/goto/{spot}")

                threading.Thread(target=_dispatch_entry, args=(safe_plate, target_spot), daemon=True).start()
            else:
                print(f"[ENTRY REJECT] Lot full for '{car_plate}'!")

        # B. Cleared entry box
        elif spot_type == "EntrySpot" and direction == "CarOut":
            print(f"[GATE] Car cleared entry box. Delaying close of '{entry_gate_name}' by 3s...")
            delayed_close_gate(entry_gate_name, delay_seconds=3.0)

        # C. Finished parking -> Direct to EXIT
        elif spot_type == "Park" and direction == "CarOut":
            with state_lock:
                reserved_spots.discard(spot_name)
            print(f"\n[PARK FINISHED] '{car_plate}' left bay '{spot_name}'. Directing to EXIT...")
            call_simulator_api("POST", f"/car/{safe_plate}/goto/exit")

        # D. Arrived at Exit Box -> Wait for full stop -> CHARGE
        elif (spot_type == "ExitSpot" or spot_name in ["EXIT", "EXIT_EXIT"]) and direction == "CarIn":
            safe_close_gate(exit_gate_name)

            car_info = active_cars.get(car_plate, {})
            elapsed_seconds = time.time() - car_info.get("entry_timestamp", time.time())
            duration = max(1, int(elapsed_seconds / 60))
            is_electric = (car_info.get("type") == "Electric")

            parking_cost = float(duration)
            charging_cost = float(duration * 2) if is_electric else 0.0
            total_fee = parking_cost + charging_cost
            car_info["expected_cost"] = total_fee

            log_car_exit(car_plate, parking_cost, charging_cost, total_fee)

            if not car_info.get("charged"):
                car_info["charged"] = True
                print(f"[EXIT] Waiting 1.5s for '{car_plate}' to come to a full physical stop before charging...")

                def _charge_after_stop(plate, p_cost, c_cost):
                    time.sleep(1.5)
                    print(f"[CHARGE] Car stopped. Requesting payment from '{plate}' (P={p_cost}, C={c_cost})...")
                    charge_params = {"parkingCost": p_cost, "chargingCost": c_cost}
                    call_simulator_api("POST", f"/car/{plate}/charge", params=charge_params)

                threading.Thread(target=_charge_after_stop, args=(safe_plate, parking_cost, charging_cost), daemon=True).start()

        # E. Cleared Exit Barrier -> Lower gateB immediately
        elif (spot_type == "ExitSpot" or spot_name in ["EXIT", "EXIT_EXIT"]) and direction == "CarOut":
            print(f"[EXIT COMPLETE] Car '{car_plate}' departed. Lowering '{exit_gate_name}'...")
            safe_close_gate(exit_gate_name)
            active_cars.pop(car_plate, None)

    # 2. Payment confirmed -> Lift gateB -> Leave Park
    elif event_class == "payment_made":
        car_plate = data.get("CarPlateNumber")
        car_info = active_cars.get(car_plate, {})
        safe_plate = urllib.parse.quote(car_plate)

        raw_amount = data.get("Amount")
        if raw_amount is None:
            raw_amount = data.get("amount")
        
        amount = float(raw_amount) if raw_amount is not None else float(car_info.get("expected_cost", 1.0))
        if amount == 0.0:
            amount = float(car_info.get("expected_cost", 1.0))

        p_cost = float(car_info.get("duration", 1.0))
        c_cost = float(car_info.get("expected_cost", amount)) - p_cost
        if c_cost < 0: 
            c_cost = 0.0

        log_car_exit(car_plate, p_cost, c_cost, amount)

        print(f"\n[PAYMENT VERIFIED] Car '{car_plate}' paid ${amount}. Lifting '{exit_gate_name}'...")
        safe_open_gate(exit_gate_name)

        def _dispatch_exit(plate):
            time.sleep(1.5)
            print(f"[EXIT DISPATCH] Arm raised. Releasing '{plate}' from park...")
            call_simulator_api("POST", f"/car/{plate}/goto/leavepark")
            time.sleep(4.0)
            safe_close_gate(exit_gate_name)

        threading.Thread(target=_dispatch_exit, args=(safe_plate,), daemon=True).start()

    # 3. Auto-Repairs
    elif event_class == "component_broken":
        c_type = data.get("Type")
        c_name = data.get("Name")
        if c_type == "BarrierGate":
            call_simulator_api("POST", f"/barrier-gates/{c_name}/repair")
        elif c_type in ["Parking", "ParkingSpot"]:
            spots = call_simulator_api("GET", "/list-parking-spots") or []
            is_empty = any(s.get("name") == c_name and is_spot_empty(s) for s in spots)
            if is_empty:
                call_simulator_api("POST", f"/parking-spots/{c_name}/repair")
        elif c_type == "ExhaustFan":
            call_simulator_api("POST", f"/exhaust-fans/{c_name}/repair")

    # 4. Carbon Monoxide safety
    elif event_class == "carbon_monoxide_event":
        danger = data.get("DangerLevel")
        if danger in ["Mid", "High", "Critical"]:
            for fan in exhaust_fans or ["fan0"]:
                call_simulator_api("POST", f"/exhaust-fans/{fan}/on")
        elif danger == "Safe":
            for fan in exhaust_fans or ["fan0"]:
                call_simulator_api("POST", f"/exhaust-fans/{fan}/off")

    # 5. Log Penalties
    elif event_class == "penalty":
        reason = data.get("Reason", "Unknown")
        fine = float(data.get("FineAmount", 0.0))
        log_penalty(reason, fine)
        print(f"\n🚨 [PENALTY INCURRED] {reason} | -{fine} credits 🚨\n")

    return jsonify({"status": "ok"}), 200


# ==============================================================================
# WEB DASHBOARD ROUTES (HTML RENDERING & AUTH)
# ==============================================================================

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
                
                if user["role"] == "admin":
                    return redirect(url_for("admin_dashboard"))
                else:
                    return redirect(url_for("operator_dashboard"))
            else:
                error = "Invalid credentials. Please try again."
                
        elif action == "signup":
            role = request.form.get("role")
            if username in users:
                error = "Username already exists! Please choose another."
            elif not username or not password:
                error = "Username and Password cannot be empty."
            else:

                users[username] = {"password": password, "role": role}
                save_users(users)
                

                session["username"] = username
                session["role"] = role
                if role == "admin":
                    return redirect(url_for("admin_dashboard"))
                else:
                    return redirect(url_for("operator_dashboard"))
            
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/", methods=["GET"])
@login_required
def root():
    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("operator_dashboard"))

@app.route("/operator", methods=["GET"])
@login_required
def operator_dashboard():
    # Render the old DASHBOARD_HTML which you put into templates/operator.html
    return render_template("operator.html", username=session.get("username"))

@app.route("/admin", methods=["GET"])
@admin_required
def admin_dashboard():
    # Render the new templates/admin.html
    return render_template("admin.html", username=session.get("username"))


# ==============================================================================
# API ENDPOINTS FOR DASHBOARD (PROTECTED)
# ==============================================================================

@app.route("/api/dashboard/status", methods=["GET"])
@login_required
def dashboard_status():
    spots = call_simulator_api("GET", "/list-parking-spots") or []
    barriers = call_simulator_api("GET", "/list-barriers") or []

    gate_a = "Unknown"
    gate_b = "Unknown"
    for b in barriers:
        if b.get("name") == "gateA": gate_a = f"{b.get('state')} {'(BROKEN)' if b.get('broken') else ''}"
        elif b.get("name") == "gateB": gate_b = f"{b.get('state')} {'(BROKEN)' if b.get('broken') else ''}"

    with state_lock:
        free_spots = sum(1 for s in spots if s.get("purpose") == "Park" and is_spot_empty(s) and not s.get("broken") and s.get("name") not in reserved_spots)
        curr_reserved = list(reserved_spots)

    valid_spots = [s for s in spots if s.get("purpose") == "Park"]
    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s.get("name", ""))]
    valid_spots.sort(key=natural_sort_key)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    
    c.execute('''
        SELECT * FROM car_logs 
        ORDER BY CASE WHEN exit_time IS NOT NULL THEN exit_time ELSE entry_time END DESC 
        LIMIT 100
    ''')
    logs = c.fetchall()
    
    c.execute("SELECT COUNT(*), COALESCE(SUM(fine_amount), 0) FROM penalty_logs")
    p_count, p_total = c.fetchone()
    
    c.execute("SELECT COALESCE(SUM(total_paid), 0) FROM car_logs WHERE status = 'Completed'")
    total_revenue = c.fetchone()[0]
    conn.close()

    return jsonify({
        "free_spots": free_spots,
        "gate_a": gate_a,
        "gate_b": gate_b,
        "spots": valid_spots,
        "reserved_spots": curr_reserved,
        "logs": logs,
        "penalties_count": p_count,
        "penalties_total": p_total,
        "total_revenue": total_revenue
    })

@app.route("/api/operator/gate/<name>/<action>", methods=["POST"])
@login_required
def operator_gate(name, action):
    if action == "repair": call_simulator_api("POST", f"/barrier-gates/{name}/repair")
    elif action == "open": safe_open_gate(name)
    elif action == "close": safe_close_gate(name)
    return jsonify({"status": "ok"})

@app.route("/api/operator/penalties/reset", methods=["POST"])
@login_required
def operator_reset_penalties():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM penalty_logs")
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================
if __name__ == "__main__":
    init_db()
    threading.Thread(target=initialize_system, daemon=True).start()

    print("=" * 65)
    print("  CAR PARK MANAGEMENT SYSTEM (CTRL ALT EVERYTHING)")
    print("  Exit Time & Fee Logging Active | Dashboard Synchronized")
    print("  Live Command Center: http://127.0.0.1:5000")
    print("=" * 65)

    app.run(host="0.0.0.0", port=5000, debug=False)
import time
import threading
import requests
import config

def is_spot_empty(spot):
    detected = spot.get("detectedCars")
    if detected is None: return True
    if isinstance(detected, int): return detected == 0
    if isinstance(detected, (list, tuple)): return len(detected) == 0
    return False

def login_to_simulator():
    """Logs in using credentials and caches Bearer JWT token."""
    with config.auth_lock:
        url = f"{config.SIMULATOR_BASE_URL}/auth/login"
        payload = {"Email": config.ADMIN_NAME, "Password": config.ADMIN_PASS}
        try:
            res = requests.post(url, json=payload, timeout=5)
            if res.status_code == 200:
                config.jwt_token = res.json().get("token")
                print(f"[AUTH] Successfully acquired JWT token.")
                return True
            print(f"[AUTH FAILED] Status {res.status_code}: {res.text}")
        except Exception as e:
            print(f"[AUTH ERROR] Cannot connect to simulator on port 9898: {e}")
        return False

def call_simulator_api(method, endpoint, payload=None, params=None):
    """Sends authenticated HTTP requests to the simulator."""
    if not config.jwt_token and not login_to_simulator():
        return None

    headers = {
        "Authorization": f"Bearer {config.jwt_token}",
        "Content-Type": "application/json"
    }
    url = f"{config.SIMULATOR_BASE_URL}{endpoint}"

    try:
        if method.upper() == "GET":
            res = requests.get(url, headers=headers, params=params, timeout=5)
        else:
            res = requests.post(url, headers=headers, json=payload or {}, params=params, timeout=5)

        if res.status_code == 401:
            print("[AUTH] Token expired. Re-authenticating...")
            if login_to_simulator():
                headers["Authorization"] = f"Bearer {config.jwt_token}"
                res = requests.request(method, url, headers=headers, json=payload, params=params, timeout=5)

        return res.json() if res.content and res.status_code in [200, 201] else {}
    except Exception as e:
        print(f"[API ERROR] Request failed for {endpoint}: {e}")
        return None

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

def allocate_parking_spot(car_type):
    spots = call_simulator_api("GET", "/list-parking-spots")
    if not isinstance(spots, list): return None

    with config.state_lock:
        usable_spots = [
            s for s in spots
            if s.get("purpose") == "Park"
            and not s.get("broken")
            and not s.get("isUnderMaintenance")
            and is_spot_empty(s)
            and s.get("name") not in config.reserved_spots
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
            config.reserved_spots.add(chosen_spot)
            print(f"[RESERVATION] Bay '{chosen_spot}' reserved for incoming {c_type} car.")
            return chosen_spot

    print(f"[ALLOCATION WARNING] No matching bay available for car type '{c_type}'!")
    return None

def initialize_system():
    time.sleep(2)
    print("\n--- [SYSTEM STARTUP: LIVE HARDWARE SYNC] ---")
    if not login_to_simulator(): return

    call_simulator_api("GET", "/test")

    fans = call_simulator_api("GET", "/list-exhaust-fans")
    if isinstance(fans, list):
        config.exhaust_fans = [f.get("name") for f in fans if "name" in f]

    # Repair broken gates on startup
    barriers = call_simulator_api("GET", "/list-barriers")
    if isinstance(barriers, list):
        for b in barriers:
            if b.get("broken", False):
                call_simulator_api("POST", f"/barrier-gates/{b.get('name')}/repair")

    # Ensure exit gate is closed by default
    safe_close_gate(config.exit_gate_name)

    # Sync already parked cars into reserved_spots
    spots = call_simulator_api("GET", "/list-parking-spots")
    if isinstance(spots, list):
        with config.state_lock:
            for s in spots:
                if s.get("purpose") == "Park" and not is_spot_empty(s):
                    config.reserved_spots.add(s.get("name"))
                    print(f"[INIT SYNC] Bay '{s.get('name')}' is already occupied.")

    print("--- [INITIALIZATION COMPLETE] ---\n")
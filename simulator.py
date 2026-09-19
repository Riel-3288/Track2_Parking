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
        config.zone_fans_map = {}
        for f in fans:
            zone = f.get("zoneParent")
            name = f.get("name")
            if zone and name:
                config.zone_fans_map.setdefault(zone, []).append(name)
    # Fallback: fill in any zone the live API didn't tag correctly
    for zone, fan_list in config.STATIC_ZONE_FANS.items():
        config.zone_fans_map.setdefault(zone, fan_list)

    # Repair broken gates on startup
    barriers = call_simulator_api("GET", "/list-barriers")
    if isinstance(barriers, list):
        config.barriers = [b.get("name") for b in barriers if b.get("name")]
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



def handle_carbon_monoxide_event(danger_level, carbon_monoxide_level, zone_name):
    """Turns exhaust fans on/off per zone based on the actual CO level, threshold = 50."""
    try:
        co_level = float(carbon_monoxide_level) if carbon_monoxide_level is not None else None
    except (TypeError, ValueError):
        co_level = None

    fans = config.zone_fans_map.get(zone_name)
    if not fans:
        print(f"[CO WARNING] No fans mapped for zone '{zone_name}' — falling back to all fans.")
        fans = config.exhaust_fans or ["fan0"]

    tag = f"[{zone_name}] " if zone_name else ""
    should_run = (co_level >= 50) if co_level is not None else (danger_level in ["Mid", "High", "Critical"])

    if should_run:
        print(f"[CO ALERT] {tag}Level={co_level} Danger='{danger_level}'. Turning fans ON: {fans}")
        for fan in fans:
            call_simulator_api("POST", f"/exhaust-fans/{fan}/on")
    else:
        print(f"[CO SAFE] {tag}Level={co_level} Danger='{danger_level}'. Turning fans OFF: {fans}")
        for fan in fans:
            call_simulator_api("POST", f"/exhaust-fans/{fan}/off")

    if zone_name:
        config.zone_fan_state[zone_name] = should_run


def poll_co_levels():
    """Background loop: periodically checks live CO levels per zone and turns
    fans on/off accordingly. Needed because webhooks only fire at Mid/High/
    Critical — the simulator never sends one when a zone drops back to Safe,
    so without this fans would stay on forever after the last alert."""
    while True:
        time.sleep(config.CO_POLL_INTERVAL_SECONDS)
        zones = call_simulator_api("GET", "/list-zones")
        if not isinstance(zones, list):
            continue

        for z in zones:
            zone_name = z.get("name")
            co_level = z.get("gasCarbonMonoxideLevel")
            if zone_name is None or co_level is None:
                continue

            should_run = co_level >= 50
            currently_on = config.zone_fan_state.get(zone_name, False)
            if should_run == currently_on:
                continue  # no change — skip API calls

            fans = config.zone_fans_map.get(zone_name) or config.exhaust_fans or ["fan0"]
            tag = f"[{zone_name}] "
            if should_run:
                print(f"[CO POLL ALERT] {tag}Level={co_level}. Turning fans ON: {fans}")
                for fan in fans:
                    call_simulator_api("POST", f"/exhaust-fans/{fan}/on")
            else:
                print(f"[CO POLL SAFE] {tag}Level={co_level}. Turning fans OFF: {fans}")
                for fan in fans:
                    call_simulator_api("POST", f"/exhaust-fans/{fan}/off")

            config.zone_fan_state[zone_name] = should_run

def start_co_polling():
    threading.Thread(target=poll_co_levels, daemon=True).start()
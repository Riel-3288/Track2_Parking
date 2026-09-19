import time
import threading
import sqlite3
import re
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
import config
import auth
import database
import simulator
import math

app = Flask(__name__)
app.secret_key = "ctrl_alt_everything_super_secret_key" 

# ==============================================================================
# WEBHOOK EVENT HANDLER
# ==============================================================================
@app.route("/webhook", methods=["GET", "POST"])
def webhook_listener():
    if request.method == "GET":
        return jsonify({"status": "online"}), 200

    data = request.get_json(force=True, silent=True) or {}
    event_id = data.get("EventId")
    event_class = data.get("EventClass")

    if event_id:
        if event_id in config.processed_event_ids:
            return jsonify({"status": "duplicate"}), 200
        config.processed_event_ids.add(event_id)

    # 1. Car movement events
    if event_class == "car_spot_action":
        car_plate = data.get("CarPlateNumber")
        spot_name = data.get("SpotName")
        spot_type = data.get("SpotType")
        direction = data.get("Direction")
        car_type = data.get("CarType", "Normal")
        planned_duration = int(data.get("PlannedParkingDurationInMinutes", 0))

        if not car_plate:
            return jsonify({"status": "ignored"}), 200

        raw_plate = car_plate.strip()
        nospace_plate = raw_plate.replace(" ", "")

        # A. Entrance Arrival -> Record planned duration & Dispatch
        if spot_type == "EntrySpot" and direction == "CarIn":
            target_spot = simulator.allocate_parking_spot(car_type)
            if target_spot:
                config.active_cars[raw_plate] = {
                    "spot": target_spot,
                    "type": car_type,
                    "duration": max(1, planned_duration),
                    "charged": False
                }
                database.log_car_entry(raw_plate, car_type, target_spot)
                print(f"\n[ENTRY] Admitting '{raw_plate}' ({car_type}) -> Bay '{target_spot}' (Planned: {planned_duration} mins)")

                simulator.safe_open_gate(config.entry_gate_name)

                def _dispatch_entry(plate, spot):
                    time.sleep(1.5)
                    print(f"[DISPATCH] Arm raised. Directing '{plate}' into '{spot}'...")
                    simulator.call_simulator_api("POST", f"/car/{plate}/goto/{spot}")

                threading.Thread(target=_dispatch_entry, args=(nospace_plate, target_spot), daemon=True).start()
            else:
                print(f"[ENTRY REJECT] Lot full for '{raw_plate}'!")

        # B. Cleared entry box -> Delay close of gateA by 3s
        elif spot_type == "EntrySpot" and direction == "CarOut":
            simulator.delayed_close_gate(config.entry_gate_name, delay_seconds=3.0)

        # C1. Car physically enters parking bay -> Capture exact planned stay if updated!
        elif spot_type == "Park" and direction == "CarIn":

                car_info = config.active_cars.setdefault(raw_plate, {})

                # Start actual parking timer when car completely enters the parking bay
                car_info["parking_start_time"] = time.time()

                print(f"[PARK DOCKED] '{raw_plate}' parked in '{spot_name}'. Simulator stay duration: {planned_duration} mins.")

        # C2. Finished parking & leaves bay -> Direct to EXIT & release reservation
        elif spot_type == "Park" and direction == "CarOut":

            car_info = config.active_cars.get(raw_plate, {})

            start_time = car_info.get("parking_start_time")

            if start_time:
                parking_seconds = time.time() - start_time
                parking_minutes = parking_seconds / 60
                car_info["actual_duration"] = parking_minutes
                print(f"actual parking duration for '{raw_plate}' was {parking_minutes:.2f} mins.")

            with config.state_lock:
                config.reserved_spots.discard(spot_name)

            print(f"\n[PARK FINISHED] '{raw_plate}' left bay '{spot_name}'. Directing to EXIT...")
            simulator.call_simulator_api("POST", f"/car/{nospace_plate}/goto/exit")

        # D. Arrived at Exit Box -> Calculate Fee from Simulator's Exact Planned Stay
        elif (spot_type == "ExitSpot" or spot_name in ["EXIT", "EXIT_EXIT"]) and direction == "CarIn":
            simulator.safe_close_gate(config.exit_gate_name)

            car_info = config.active_cars.get(raw_plate, {})
            duration = max(1, math.ceil(car_info.get("actual_duration", 1)))
            is_electric = (car_info.get("type") == "Electric")

            parking_cost = float(duration)
            charging_cost = float(duration) if is_electric else 0.0
            total_fee = parking_cost + charging_cost
            print(f"The total fee for '{raw_plate}' is ${total_fee:.2f} (Parking=${parking_cost:.2f}, Charging=${charging_cost:.2f})")

            car_info["parking_cost"] = parking_cost
            car_info["charging_cost"] = charging_cost
            car_info["expected_cost"] = total_fee

            # Record exit time and fee in database
            database.log_car_exit(raw_plate, parking_cost, charging_cost, total_fee)

            if not car_info.get("charged"):
                car_info["charged"] = True
                print(f"[EXIT] '{raw_plate}' fee: {duration} mins -> Parking=${parking_cost:.2f}, Charging=${charging_cost:.2f}")

                def _charge_after_stop(plate, p_cost, c_cost):
                    time.sleep(1.5)  # Wait for car to come to a complete halt
                    print(f"[CHARGE] Requesting payment from '{plate}' (P={p_cost}, C={c_cost})...")
                    charge_params = {"parkingCost": p_cost, "chargingCost": c_cost}
                    simulator.call_simulator_api("POST", f"/car/{plate}/charge", payload=charge_params, params=charge_params)

                threading.Thread(target=_charge_after_stop, args=(nospace_plate, parking_cost, charging_cost), daemon=True).start()

        # E. Cleared Exit Barrier -> Lower gateB immediately
        elif (spot_type == "ExitSpot" or spot_name in ["EXIT", "EXIT_EXIT"]) and direction == "CarOut":
            print(f"[EXIT COMPLETE] Car '{raw_plate}' departed. Lowering '{config.exit_gate_name}'...")
            simulator.safe_close_gate(config.exit_gate_name)
            config.active_cars.pop(raw_plate, None)

    # 2. Payment confirmed -> Lift gateB, wait 1.5s, dispatch to leavepark, auto-close!
    elif event_class == "payment_made":
        car_plate = data.get("CarPlateNumber", "").strip()
        nospace_plate = car_plate.replace(" ", "")
        car_info = config.active_cars.get(car_plate, {})

        raw_amount = data.get("Amount") or data.get("amount")
        amount = float(raw_amount) if raw_amount is not None else float(car_info.get("expected_cost", 1.0))
        if amount == 0.0:
            amount = float(car_info.get("expected_cost", 1.0))

        p_cost = float(car_info.get("parking_cost", 1.0))
        c_cost = float(car_info.get("charging_cost", 0.0))

        # Update database with exact paid amount
        database.log_car_exit(car_plate, p_cost, c_cost, amount)

        print(f"\n[PAYMENT VERIFIED] Car '{car_plate}' paid ${amount}. Lifting '{config.exit_gate_name}'...")
        simulator.safe_open_gate(config.exit_gate_name)

        def _dispatch_exit(plate):
            time.sleep(1.5)  # Wait for gateB arm to physically rise
            print(f"[EXIT DISPATCH] Arm raised. Releasing '{plate}' from park...")
            simulator.call_simulator_api("POST", f"/car/{plate}/goto/leavepark")
            time.sleep(4.0)
            simulator.safe_close_gate(config.exit_gate_name)

        threading.Thread(target=_dispatch_exit, args=(nospace_plate,), daemon=True).start()

    # 3. Auto-Repairs
    elif event_class == "component_broken":
        c_type = data.get("Type")
        c_name = data.get("Name")
        if c_type == "BarrierGate":
            simulator.call_simulator_api("POST", f"/barrier-gates/{c_name}/repair")
        elif c_type in ["Parking", "ParkingSpot"]:
            spots = simulator.call_simulator_api("GET", "/list-parking-spots") or []
            is_empty = any(s.get("name") == c_name and simulator.is_spot_empty(s) for s in spots)
            if is_empty:
                simulator.call_simulator_api("POST", f"/parking-spots/{c_name}/repair")
        elif c_type == "ExhaustFan":
            simulator.call_simulator_api("POST", f"/exhaust-fans/{c_name}/repair")

    # 4. Carbon Monoxide safety
    elif event_class == "carbon_monoxide_event":

        danger = data.get("DangerLevel")
        if danger in ["Mid", "High", "Critical"]:
            for fan in config.exhaust_fans or ["fan0"]:
                simulator.call_simulator_api("POST", f"/exhaust-fans/{fan}/on")
        elif danger == "Safe":
            for fan in config.exhaust_fans or ["fan0"]:
                simulator.call_simulator_api("POST", f"/exhaust-fans/{fan}/off")

    # 5. Log Penalties
    elif event_class == "penalty":
        reason = data.get("Reason", "Unknown")
        fine = float(data.get("FineAmount", 0.0))
        database.log_penalty(reason, fine)
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
        
        users = auth.load_users()
        
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
                auth.save_users(users)
                
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
@auth.login_required
def root():
    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("operator_dashboard"))

@app.route("/operator", methods=["GET"])
@auth.login_required
def operator_dashboard():
    return render_template("operator.html", username=session.get("username"))

@app.route("/admin", methods=["GET"])
@auth.admin_required
def admin_dashboard():
    return render_template("admin.html", username=session.get("username"))


# ==============================================================================
# API ENDPOINTS FOR DASHBOARD (PROTECTED)
# ==============================================================================
@app.route("/api/dashboard/status", methods=["GET"])
@auth.login_required
def dashboard_status():
    spots = simulator.call_simulator_api("GET", "/list-parking-spots") or []
    barriers = simulator.call_simulator_api("GET", "/list-barriers") or []

    gate_a = "Unknown"
    gate_b = "Unknown"
    for b in barriers:
        if b.get("name") == "gateA": gate_a = f"{b.get('state')} {'(BROKEN)' if b.get('broken') else ''}"
        elif b.get("name") == "gateB": gate_b = f"{b.get('state')} {'(BROKEN)' if b.get('broken') else ''}"

    with config.state_lock:
        free_spots = sum(1 for s in spots if s.get("purpose") == "Park" and simulator.is_spot_empty(s) and not s.get("broken") and s.get("name") not in config.reserved_spots)
        curr_reserved = list(config.reserved_spots)

    valid_spots = [s for s in spots if s.get("purpose") == "Park"]
    def natural_sort_key(s):
        return [int(text) if text.isdigit() else text.lower() for text in re.split('([0-9]+)', s.get("name", ""))]
    valid_spots.sort(key=natural_sort_key)

    conn = sqlite3.connect(config.DB_FILE)
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
@auth.login_required
def operator_gate(name, action):
    if action == "repair": simulator.call_simulator_api("POST", f"/barrier-gates/{name}/repair")
    elif action == "open": simulator.safe_open_gate(name)
    elif action == "close": simulator.safe_close_gate(name)
    return jsonify({"status": "ok"})

@app.route("/api/operator/penalties/reset", methods=["POST"])
@auth.login_required
def operator_reset_penalties():
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM penalty_logs")
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================
if __name__ == "__main__":
    database.init_db()
    threading.Thread(target=simulator.initialize_system, daemon=True).start()

    print("=" * 65)
    print("  CAR PARK MANAGEMENT SYSTEM (CTRL ALT EVERYTHING)")
    print("  Exit Time & Fee Logging Active | Dashboard Synchronized")
    print("  Live Command Center: http://127.0.0.1:5000")
    print("=" * 65)

    simulator.handle_carbon_monoxide_event("Safe")

    app.run(host="0.0.0.0", port=5000, debug=False)
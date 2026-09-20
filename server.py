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
import json

app = Flask(__name__)
app.secret_key = "ctrl_alt_everything_super_secret_key" 

# ==============================================================================
# WEBHOOK EVENT HANDLER
# ==============================================================================
def get_gate_for_spot(spot_name, is_entry=True):
    return config.entry_gate_name if is_entry else config.exit_gate_name

@app.route("/webhook", methods=["GET", "POST"])
def webhook_listener():
    if request.method == "GET":
        return jsonify({"status": "online"}), 200

    raw_body = request.get_data()
    try:
        data = json.loads(raw_body, parse_float=str, parse_int=str)
    except Exception:
        data = {}
    event_class = data.get("EventClass")

    ok, reason = auth.verify_webhook_signature(data)
    database.log_webhook_call(request.remote_addr, event_class, bool(data.get("Signature")), reason)

    if not ok:
        print(f"⚠️  [UNSIGNED/INVALID WEBHOOK] from {request.remote_addr} | {event_class} | {reason}")
        if config.WEBHOOK_REQUIRE_SIGNATURE:
            return jsonify({"status": "error", "message": "invalid or missing signature"}), 401

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
            target_gate = get_gate_for_spot(spot_name, is_entry=True)  # 智能找门！
            
            if target_spot:
                config.active_cars[raw_plate] = {
                    "spot": target_spot,
                    "type": car_type,
                    "duration": max(1, planned_duration),
                    "charged": False
                }
                database.log_car_entry(raw_plate, car_type, target_spot)
                print(f"\n[ENTRY] Car '{raw_plate}' at '{spot_name}' -> Auto-opening '{target_gate}', Bay '{target_spot}'")

                simulator.safe_open_gate(target_gate)

                def _dispatch_entry(plate, spot):
                    time.sleep(1.5)
                    print(f"[DISPATCH] Arm raised. Directing '{plate}' into '{spot}'...")
                    simulator.call_simulator_api("POST", f"/car/{plate}/goto/{spot}")

                threading.Thread(target=_dispatch_entry, args=(nospace_plate, target_spot), daemon=True).start()
            else:
                print(f"[ENTRY REJECT] Lot full for '{raw_plate}'!")

        # B. Cleared entry box -> Delay close
        elif spot_type == "EntrySpot" and direction == "CarOut":
            target_gate = get_gate_for_spot(spot_name, is_entry=True)
            simulator.delayed_close_gate(target_gate, delay_seconds=3.0)

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
            target_gate = get_gate_for_spot(spot_name, is_entry=False)
            simulator.safe_close_gate(target_gate)

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
                print(f"[EXIT WAIT] '{raw_plate}' at '{spot_name}' (Gate: {target_gate}) -> Fee: ${total_fee:.2f}")

                def _charge_after_stop(plate, p_cost, c_cost):
                    time.sleep(1.5)  # Wait for car to come to a complete halt
                    print(f"[CHARGE] Requesting payment from '{plate}' (P={p_cost}, C={c_cost})...")
                    charge_params = {"parkingCost": p_cost, "chargingCost": c_cost}
                    simulator.call_simulator_api("POST", f"/car/{plate}/charge", payload=charge_params, params=charge_params)

                threading.Thread(target=_charge_after_stop, args=(nospace_plate, parking_cost, charging_cost), daemon=True).start()

        # E. Cleared Exit Barrier -> Lower gate immediately
        elif (spot_type == "ExitSpot" or spot_name in ["EXIT", "EXIT_EXIT"]) and direction == "CarOut":
            target_gate = get_gate_for_spot(spot_name, is_entry=False)
            print(f"[EXIT COMPLETE] Car '{raw_plate}' departed. Lowering '{target_gate}'...")
            simulator.safe_close_gate(target_gate)
            config.active_cars.pop(raw_plate, None)

    # 2. Payment confirmed -> Lift exit gate, wait 1.5s, dispatch to leavepark, auto-close!
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

        database.log_car_exit(car_plate, p_cost, c_cost, amount)

        # 取出刚才存下的真实 Exit Gate
        target_gate = car_info.get("exit_gate", config.exit_gate_name)
        print(f"\n[PAYMENT VERIFIED] Car '{car_plate}' paid ${amount}. Lifting '{target_gate}'...")
        simulator.safe_open_gate(target_gate)

        def _dispatch_exit(plate, gate):
            time.sleep(1.5)  # Wait for gate arm to physically rise
            print(f"[EXIT DISPATCH] Arm raised. Releasing '{plate}' from park...")
            simulator.call_simulator_api("POST", f"/car/{plate}/goto/leavepark")
            time.sleep(4.0)
            simulator.safe_close_gate(gate)

        threading.Thread(target=_dispatch_exit, args=(nospace_plate, target_gate), daemon=True).start()

    # 3. Auto-Repairs
    elif event_class == "component_broken":
        c_type = data.get("Type")
        c_name = data.get("Name")
        if c_type == "BarrierGate":
            simulator.call_simulator_api("POST", f"/barrier-gates/{c_name}/repair")
            database.log_audit("system (webhook)", "auto_repair", c_name, f"type={c_type}")
        elif c_type in ["Parking", "ParkingSpot"]:
            spots = simulator.call_simulator_api("GET", "/list-parking-spots") or []
            is_empty = any(s.get("name") == c_name and simulator.is_spot_empty(s) for s in spots)
            if is_empty:
                simulator.call_simulator_api("POST", f"/parking-spots/{c_name}/repair")
                database.log_audit("system (webhook)", "auto_repair", c_name, f"type={c_type}")
        elif c_type == "ExhaustFan":
            simulator.call_simulator_api("POST", f"/exhaust-fans/{c_name}/repair")
            database.log_audit("system (webhook)", "auto_repair", c_name, f"type={c_type}")
        elif c_type == "Light":
            simulator.call_simulator_api("POST", f"/lights/{c_name}/repair")
            database.log_audit("system (webhook)", "auto_repair", c_name, f"type={c_type}")
        elif c_type == "Display":
            simulator.call_simulator_api("POST", f"/displays/{c_name}/repair")
            database.log_audit("system (webhook)", "auto_repair", c_name, f"type={c_type}")
        elif event_class == "component_fixed":
            c_type = data.get("Type")
            c_name = data.get("Name")
            repair_cost = data.get("RepairCost")
            database.log_audit("system (webhook)", "component_fixed", c_name, f"type={c_type}, cost={repair_cost}")
        elif event_class == "carbon_monoxide_event":
            danger_level = data.get("DangerLevel")
            database.log_audit("system (webhook)", "co_alert", "exhaust_fans", f"danger_level={danger_level}")
            simulator.handle_carbon_monoxide_event(danger_level)

    # 4. Carbon Monoxide safety
    elif event_class == "carbon_monoxide_event":
        simulator.handle_carbon_monoxide_event(
            data.get("DangerLevel"),
            data.get("CarbonMonoxideLevel"),
            data.get("ZoneName")
    )

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
        ip = request.remote_addr

        users = auth.load_users()

        if action == "login":
            user = users.get(username)
            if user and user["password"] == password:
                database.log_login_attempt(username, True, ip, "login_success")
                session["username"] = username
                session["role"] = user["role"]
                # 不再需要 session["last_logins"] = history 这一行，删掉即可

                if user["role"] == "admin":
                    return redirect(url_for("admin_dashboard"))
                else:
                    return redirect(url_for("operator_dashboard"))
            else:
                reason = "unknown_user" if not user else "wrong_password"
                database.log_login_attempt(username or "(blank)", False, ip, reason)
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

                database.log_login_attempt(username, True, ip, "signup")
                session["username"] = username
                session["role"] = role
                session["last_logins"] = []

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
    return render_template(
        "operator.html",
        username=session.get("username"),
        role=session.get("role"),
        last_logins=database.get_recent_logins(session.get("username"), 3)
    )

@app.route("/admin", methods=["GET"])
@auth.admin_required
def admin_dashboard():
    return render_template(
        "admin.html",
        username=session.get("username"),
        role=session.get("role"),
        last_logins=database.get_recent_logins(session.get("username"), 3)
    )

# ==============================================================================
# API ENDPOINTS FOR DASHBOARD (PROTECTED)
# ==============================================================================
@app.route("/api/admin/audit-logs", methods=["GET"])
@auth.admin_required
def audit_logs_api():
    return jsonify({"logs": database.get_audit_logs(200)})


@app.route("/penalties", methods=["GET"])
@auth.login_required
def penalties_page():
    return render_template(
        "penalties.html",
        username=session.get("username"),
        role=session.get("role")
    )

@app.route("/api/penalties", methods=["GET"])
@auth.login_required
def api_penalties():
    return jsonify({"penalties": database.get_all_penalties(300)})


@app.route("/api/dashboard/status", methods=["GET"])
@auth.login_required
def dashboard_status():
    spots = simulator.call_simulator_api("GET", "/list-parking-spots") or []
    barriers = simulator.call_simulator_api("GET", "/list-barriers") or []

    gate_a = "Unknown"
    gate_b = "Unknown"
    for b in barriers:
        if b.get("name") == config.entry_gate_name: 
            gate_a = f"{b.get('state')} {'(BROKEN)' if b.get('broken') else ''}"
        elif b.get("name") == config.exit_gate_name: 
            gate_b = f"{b.get('state')} {'(BROKEN)' if b.get('broken') else ''}"

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
    real_gate_name = name
    if name == "gateA":
        real_gate_name = config.entry_gate_name
    elif name == "gateB":
        real_gate_name = config.exit_gate_name

    if action in ["open", "close"]:
        if not auth.has_permission("can_control_gate"):
            return jsonify({"status": "error", "message": "No permission to control gates"}), 403

        if action == "open": simulator.safe_open_gate(real_gate_name)
        elif action == "close": simulator.safe_close_gate(real_gate_name)
        database.log_audit(session.get("username"), f"gate_{action}", real_gate_name)

    elif action == "repair":
        if not auth.has_permission("can_repair"):
            return jsonify({"status": "error", "message": "Only authorized technicians (Admin) can perform repairs."}), 403

        simulator.call_simulator_api("POST", f"/barrier-gates/{real_gate_name}/repair")
        database.log_audit(session.get("username"), "gate_repair", real_gate_name)
    else:
        return jsonify({"status": "error", "message": "Unknown action"}), 400

    return jsonify({"status": "ok"})

@app.route("/api/operator/penalties/reset", methods=["POST"])
@auth.permission_required("can_reset_penalties") # only admins can reset penalties
def operator_reset_penalties():
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM penalty_logs")
    count = c.fetchone()[0]
    c.execute("DELETE FROM penalty_logs")
    conn.commit()
    conn.close()

    database.log_audit(session.get("username"), "reset_penalties", "penalty_logs", f"cleared {count} record(s)")
    return jsonify({"status": "ok"})

# Financial report endpoint for admins
@app.route("/api/admin/financial-report", methods=["GET"])
@auth.permission_required("can_generate_reports")
def generate_financial_report():
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    # Calculate total revenue from completed car logs
    c.execute("SELECT SUM(parking_cost), SUM(charging_cost), SUM(total_paid) FROM car_logs WHERE status = 'Completed'")
    rev_row = c.fetchone()
    
    c.execute("SELECT SUM(fine_amount) FROM penalty_logs")
    pen_row = c.fetchone()
    conn.close()

    return jsonify({
        "status": "success",
        "report": {
            "total_parking_revenue": rev_row[0] or 0.0,
            "total_charging_revenue": rev_row[1] or 0.0,
            "gross_revenue": rev_row[2] or 0.0,
            "total_penalties_paid": pen_row[0] or 0.0,
            "net_profit": (rev_row[2] or 0.0) - (pen_row[0] or 0.0)
        }
    })

# Login attempt history API for admins
@app.route("/api/admin/webhook-logs", methods=["GET"])
@auth.admin_required
def webhook_logs_api():
    verdict_filter = request.args.get("verdict", "All")
    date_filter = request.args.get("date")   # 格式 "YYYY-MM-DD"，前端 date input 原生就是这个格式
    logs = database.get_recent_webhook_logs(limit=50, verdict_filter=verdict_filter, date_filter=date_filter)
    return jsonify({"logs": logs})

# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================
if __name__ == "__main__":
    database.init_db()
    
    simulator.initialize_system() 
    simulator.start_co_polling() 

    print("=" * 65)
    print("  CAR PARK MANAGEMENT SYSTEM (CTRL ALT EVERYTHING)")
    print("  Exit Time & Fee Logging Active | Dashboard Synchronized")
    print("  Live Command Center: http://127.0.0.1:5000")
    print("=" * 65)

    for zone in config.STATIC_ZONE_FANS:
        simulator.handle_carbon_monoxide_event("Safe", 0, zone)

    app.run(host="0.0.0.0", port=5000, debug=False)
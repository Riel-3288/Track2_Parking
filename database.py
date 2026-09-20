import sqlite3
import config
from datetime import datetime

def log_webhook_call(remote_addr, event_class, signed, verdict):
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO webhook_logs (remote_addr, event_class, signed, verdict, received_at) VALUES (?,?,?,?,?)",
              (remote_addr, event_class, 1 if signed else 0, verdict,
               datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def init_db():
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS login_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        success INTEGER,
        ip_address TEXT,
        reason TEXT,
        attempt_time TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS webhook_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        remote_addr TEXT,
        event_class TEXT,
        signed INTEGER,
        verdict TEXT,
        received_at TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS login_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        success INTEGER,
        ip_address TEXT,
        reason TEXT,
        attempt_time TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor TEXT,
        action TEXT,
        target TEXT,
        details TEXT,
        logged_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS penalty_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        reason TEXT,
        fine_amount REAL,
        timestamp TEXT
    )''')  

    conn.commit()
    conn.close()

def log_car_entry(plate, car_type, spot):
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO car_logs (plate, car_type, spot_name, entry_time, status)
                 VALUES (?, ?, ?, datetime('now', 'localtime'), 'Parked')''',
              (plate.strip(), car_type, spot))
    conn.commit()
    conn.close()

def log_car_exit(plate, p_cost, c_cost, total):
    """Updates database with exit timestamp, calculated fees, and marks Completed."""
    conn = sqlite3.connect(config.DB_FILE)
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
                     WHERE id = ?''',
                  (p_cost, c_cost, total, row[0]))
    else:
        c.execute('''INSERT INTO car_logs (plate, car_type, spot_name, entry_time, exit_time, parking_cost, charging_cost, total_paid, status)
                     VALUES (?, 'Normal', 'Exit', datetime('now', '-2 minutes', 'localtime'), datetime('now', 'localtime'), ?, ?, ?, 'Completed')''',
                  (clean_plate, p_cost, c_cost, total))

    conn.commit()
    conn.close()
    print(f"[DB LOGGED] Exit time & fee saved for '{clean_plate}': ${total:.2f} (Status: Completed)")

def log_penalty(reason, fine):
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    c.execute('''INSERT INTO penalty_logs (reason, fine_amount, timestamp)
                 VALUES (?, ?, datetime('now', 'localtime'))''',
              (reason, fine))
    conn.commit()
    conn.close()


def log_login_attempt(username, success, ip, reason=""):
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO login_logs (username, success, ip_address, reason, attempt_time) VALUES (?,?,?,?,?)",
        (username, 1 if success else 0, ip, reason, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()


def get_recent_logins(username, limit=3):
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT attempt_time, success, ip_address, reason FROM login_logs WHERE username = ? ORDER BY id DESC LIMIT ?",
        (username, limit)
    )
    rows = c.fetchall()
    conn.close()
    return [{"time": r[0], "success": bool(r[1]), "ip": r[2], "reason": r[3]} for r in rows]


def get_recent_webhook_logs(limit=50, verdict_filter="All", date_filter=None):
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    query = "SELECT event_class, signed, verdict, remote_addr, received_at FROM webhook_logs WHERE 1=1"
    params = []

    if verdict_filter == "Blocked":
        query += " AND verdict NOT IN ('verified', 'signature_present')"
    elif verdict_filter == "Verified":
        query += " AND verdict IN ('verified', 'signature_present')"

    if date_filter:
        query += " AND received_at LIKE ?"
        params.append(f"{date_filter}%")

    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    return [{"event_class": r[0], "signed": bool(r[1]), "verdict": r[2], "ip": r[3], "time": r[4]} for r in rows]


def log_audit(actor, action, target="", details=""):
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO audit_logs (actor, action, target, details, logged_at) VALUES (?,?,?,?,?)",
        (actor, action, target, details, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()

def get_audit_logs(limit=200):
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    c.execute("SELECT actor, action, target, details, logged_at FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"actor": r[0], "action": r[1], "target": r[2], "details": r[3], "time": r[4]} for r in rows]

def get_all_penalties(limit=300):
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    # Change logged_at to timestamp
    c.execute("SELECT id, reason, fine_amount, timestamp FROM penalty_logs ORDER BY id DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "reason": r[1], "amount": r[2], "time": r[3]} for r in rows]

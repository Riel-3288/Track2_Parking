import sqlite3
import config
from datetime import datetime

def log_login_attempt(username, success, ip, reason=""):
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO login_logs (username, success, ip_address, reason, attempt_time) VALUES (?,?,?,?,?)",
              (username, 1 if success else 0, ip, reason,
               datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def get_recent_logins(username, limit=3):
    conn = sqlite3.connect(config.DB_FILE)
    c = conn.cursor()
    c.execute("""SELECT attempt_time, success, ip_address, reason
                 FROM login_logs WHERE username = ?
                 ORDER BY id DESC LIMIT ?""", (username, limit))
    rows = c.fetchall()
    conn.close()
    return [{"time": r[0], "success": bool(r[1]), "ip": r[2], "reason": r[3]} for r in rows]

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
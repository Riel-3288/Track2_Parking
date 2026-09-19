import sqlite3
import config

def init_db():
    conn = sqlite3.connect(config.DB_FILE)
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
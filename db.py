import sqlite3
from datetime import datetime, date

DB_NAME = "supplements.db"

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS intake_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                supplement TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                notes TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()

def log_intake(supplement: str, status: str = "taken", notes: str = "") -> str:
    now = datetime.now().isoformat()
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO intake_logs (supplement, timestamp, status, notes) VALUES (?, ?, ?, ?)",
            (supplement.lower(), now, status, notes)
        )
        conn.commit()
    return f"Logged {supplement} as {status}."

def get_streak(supplement: str) -> int:
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT date(timestamp) FROM intake_logs WHERE supplement = ? AND status = 'taken' ORDER BY date(timestamp) DESC",
            (supplement.lower(),)
        )
        rows = cursor.fetchall()

    if not rows:
        return 0

    streak = 0
    today = date.today()
    for i, row in enumerate(rows):
        entry_date = date.fromisoformat(row[0])
        diff = (today - entry_date).days
        if diff == i or diff == i + 1:
            streak += 1
        else:
            break
    return streak

init_db()
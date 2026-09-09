import sqlite3
import secrets
import re

DB = "schedule.db"

def columns(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]

def add_column(db, table, name, spec):
    if name not in columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")

def unique_username(db, base):
    base = re.sub(r"[^A-Za-z0-9_.-]", "", (base or "").strip())[:30] or "user"
    candidate = base
    n = 2
    while db.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE", (candidate,)).fetchone():
        suffix = str(n)
        candidate = f"{base[:30-len(suffix)]}{suffix}"
        n += 1
    return candidate

def main():
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT NOT NULL UNIQUE COLLATE NOCASE,password_hash TEXT NOT NULL,created_at TEXT NOT NULL)")
    add_column(db, "users", "username", "TEXT")
    for row in db.execute("SELECT id,email FROM users WHERE username IS NULL OR username='' ORDER BY id").fetchall():
        base = (row["email"] or "").split("@", 1)[0]
        db.execute("UPDATE users SET username=? WHERE id=?", (unique_username(db, base), row["id"]))
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE)")
    add_column(db, "users", "share_token", "TEXT")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_share_token ON users(share_token)")
    for row in db.execute("SELECT id FROM users WHERE share_token IS NULL OR share_token='' ").fetchall():
        token = secrets.token_urlsafe(18)
        while db.execute("SELECT 1 FROM users WHERE share_token=?", (token,)).fetchone():
            token = secrets.token_urlsafe(18)
        db.execute("UPDATE users SET share_token=? WHERE id=?", (token, row["id"]))
    db.execute("CREATE TABLE IF NOT EXISTS semesters(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,name TEXT NOT NULL,start_date TEXT,end_date TEXT,is_current INTEGER DEFAULT 0)")
    db.execute("CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,class_id INTEGER,title TEXT NOT NULL,description TEXT DEFAULT '',due_at TEXT,priority INTEGER DEFAULT 1,kind TEXT DEFAULT 'task',done INTEGER DEFAULT 0,starred INTEGER DEFAULT 0,created_at TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS holidays(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,name TEXT NOT NULL,start_date TEXT NOT NULL,end_date TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS personal_events(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,title TEXT NOT NULL,start_at TEXT NOT NULL,end_at TEXT,location TEXT DEFAULT '',notes TEXT DEFAULT '')")
    db.execute("CREATE TABLE IF NOT EXISTS attendance(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,class_id INTEGER NOT NULL,class_date TEXT NOT NULL,status TEXT NOT NULL,UNIQUE(user_id,class_id,class_date))")
    db.execute("CREATE TABLE IF NOT EXISTS grades(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,class_id INTEGER NOT NULL,title TEXT NOT NULL,score REAL NOT NULL,max_score REAL NOT NULL,weight REAL DEFAULT 0,created_at TEXT NOT NULL)")
    db.commit(); db.close(); print("Database migration complete.")

if __name__ == "__main__": main()

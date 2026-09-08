from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import sqlite3, os, smtplib
from functools import wraps
from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
DATABASE = "schedule.db"
PHILIPPINES = ZoneInfo("Asia/Manila")
INSTITUTIONAL_DOMAIN = "@ndmu.edu.ph"
load_dotenv()
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD")
app.secret_key = os.getenv("SECRET_KEY") or "change-this-secret-key-in-production"


def get_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    return db


def valid_ndmu_email(email):
    email = (email or "").lower().strip()
    return email.endswith(INSTITUTIONAL_DOMAIN) and len(email) > len(INSTITUTIONAL_DOMAIN)


def current_user_id():
    return session.get("user_id")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user_id():
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def get_current_user():
    if not current_user_id():
        return None
    db = get_db()
    user = db.execute("SELECT id, email FROM users WHERE id = ?", (current_user_id(),)).fetchone()
    db.close()
    return user


def migrate_notification_settings(db):
    """Migrate the original single-row settings table safely to per-user settings."""
    table = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='notification_settings'"
    ).fetchone()

    new_schema = """
        CREATE TABLE notification_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            email TEXT DEFAULT '',
            minutes_before INTEGER NOT NULL DEFAULT 30
        )
    """

    if not table:
        db.execute(new_schema)
        return

    columns = [r["name"] for r in db.execute("PRAGMA table_info(notification_settings)").fetchall()]
    if "user_id" in columns:
        # Already migrated.
        return

    # Old schema: preserve the data under a temporary legacy table, then create
    # the multi-user table. claim_legacy_data() moves it to the first account.
    legacy_exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_settings_legacy'"
    ).fetchone()
    if legacy_exists:
        db.execute("DROP TABLE notification_settings_legacy")
    db.execute("ALTER TABLE notification_settings RENAME TO notification_settings_legacy")
    db.execute(new_schema)


def init_db():
    db = get_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day_of_week INTEGER NOT NULL,
            subject TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            room TEXT
        )
    """)
    columns = [r["name"] for r in db.execute("PRAGMA table_info(classes)").fetchall()]
    if "user_id" not in columns:
        db.execute("ALTER TABLE classes ADD COLUMN user_id INTEGER")

    migrate_notification_settings(db)

    db.execute("""
        CREATE TABLE IF NOT EXISTS sent_reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id INTEGER NOT NULL,
            class_date TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            UNIQUE(class_id, class_date)
        )
    """)

    user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    class_count = db.execute("SELECT COUNT(*) FROM classes").fetchone()[0]
    if user_count == 0 and class_count == 0:
        add_initial_schedule(db)
    db.commit()
    db.close()


def add_initial_schedule(db):
    classes = [
        (0,"SSP 114","10:30","11:30","DOHERTY V310"),(0,"CSPC 103","13:30","16:00","3013A"),(0,"ENGL 103","16:30","17:30","DOHERTY V122"),
        (1,"MST 112","07:30","09:00","DOHERTY V218"),(1,"RE 113","09:00","10:30","CREEGAN 14"),(1,"CSCC 104","13:00","15:30","NOT SET"),(1,"CSMATH 3","17:30","19:00","NOT SET"),
        (2,"SSP 114","10:30","11:30","DOHERTY V310"),(2,"CSPC 103","13:30","16:00","3013A"),(2,"ENGL 103","16:30","17:30","DOHERTY V122"),
        (3,"MST 112","07:30","09:00","DOHERTY V218"),(3,"RE 113","09:00","10:30","CREEGAN 14"),(3,"CSCC 104","13:00","15:30","NOT SET"),(3,"CSMATH 3","17:30","19:00","NOT SET"),
        (4,"PE 3","08:30","10:30","GYM 1"),(4,"SSP 114","10:30","11:30","DOHERTY V310"),(4,"CSPC 102","13:30","16:00","3017A"),(4,"ENGL 103","16:30","17:30","DOHERTY V122")]
    db.executemany("INSERT INTO classes (day_of_week,subject,start_time,end_time,room) VALUES (?,?,?,?,?)", classes)


def claim_legacy_data(user_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM classes WHERE user_id = ? LIMIT 1", (user_id,)).fetchone():
        db.execute("UPDATE classes SET user_id = ? WHERE user_id IS NULL", (user_id,))

    legacy = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='notification_settings_legacy'").fetchone()
    user = db.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    if legacy and user:
        old = db.execute("SELECT enabled,email,minutes_before FROM notification_settings_legacy LIMIT 1").fetchone()
        if old:
            old_email = (old["email"] or "").strip().lower()
            email = old_email if valid_ndmu_email(old_email) else user["email"]
            db.execute(
                "INSERT OR IGNORE INTO notification_settings (user_id,enabled,email,minutes_before) VALUES (?,?,?,?)",
                (user_id, old["enabled"], email, old["minutes_before"])
            )
        db.execute("DROP TABLE notification_settings_legacy")

    db.execute(
        "INSERT OR IGNORE INTO notification_settings (user_id,enabled,email,minutes_before) VALUES (?,0,?,30)",
        (user_id, user["email"] if user else "")
    )
    db.commit()
    db.close()


def ensure_user_settings(user_id):
    db = get_db()
    user = db.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
    if user:
        db.execute(
            "INSERT OR IGNORE INTO notification_settings (user_id,enabled,email,minutes_before) VALUES (?,0,?,30)",
            (user_id, user["email"])
        )
    db.commit()
    db.close()


def get_next_class(user_id):
    now = datetime.now(PHILIPPINES)
    db = get_db()
    classes = db.execute(
        "SELECT * FROM classes WHERE user_id=? ORDER BY day_of_week,start_time", (user_id,)
    ).fetchall()
    db.close()
    candidates = []
    for c in classes:
        date = now.date() + timedelta(days=(c["day_of_week"] - now.weekday()) % 7)
        start = datetime.strptime(c["start_time"], "%H:%M").time()
        dt = datetime.combine(date, start).replace(tzinfo=PHILIPPINES)
        if dt > now:
            candidates.append((dt, c))
    if not candidates:
        return None
    dt, c = min(candidates, key=lambda x: x[0])
    return {"class": c, "datetime": dt}


def send_email(recipient, subject, body):
    try:
        msg = EmailMessage()
        msg["From"] = EMAIL_ADDRESS
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"Email failed: {e}")
        return False


def check_for_reminders():
    now = datetime.now(PHILIPPINES)
    db = get_db()
    settings_rows = db.execute(
        "SELECT * FROM notification_settings WHERE enabled=1 AND email!=''"
    ).fetchall()
    for s in settings_rows:
        for offset in range(2):
            date = now.date() + timedelta(days=offset)
            classes = db.execute(
                "SELECT * FROM classes WHERE user_id=? AND day_of_week=? ORDER BY start_time",
                (s["user_id"], date.weekday())
            ).fetchall()
            for c in classes:
                start = datetime.strptime(c["start_time"], "%H:%M").time()
                dt = datetime.combine(date, start).replace(tzinfo=PHILIPPINES)
                reminder = dt - timedelta(minutes=s["minutes_before"])
                delta = (reminder - now).total_seconds()
                already = db.execute(
                    "SELECT id FROM sent_reminders WHERE class_id=? AND class_date=?",
                    (c["id"], date.isoformat())
                ).fetchone()
                if -90 < delta <= 0 and not already:
                    body = (
                        "Class Schedule Reminder\n\n"
                        f"Subject: {c['subject']}\n\n"
                        f"Time: {c['start_time']} - {c['end_time']}\n\n"
                        f"Room: {c['room']}\n\n"
                        f"This class starts in approximately {s['minutes_before']} minutes."
                    )
                    if send_email(s["email"], f"Class Reminder: {c['subject']}", body):
                        try:
                            db.execute(
                                "INSERT INTO sent_reminders (class_id,class_date,sent_at) VALUES (?,?,?)",
                                (c["id"], date.isoformat(), now.isoformat())
                            )
                            db.commit()
                        except sqlite3.IntegrityError:
                            pass
    db.close()


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user_id():
        return redirect(url_for("home"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        db.close()
        if not valid_ndmu_email(email) or not user or not check_password_hash(user["password_hash"], password):
            flash("Use a valid @ndmu.edu.ph account and password.", "error")
            return render_template("login.html")
        session.clear()
        session["user_id"] = user["id"]
        return redirect(url_for("home"))
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user_id():
        return redirect(url_for("home"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if not valid_ndmu_email(email):
            flash("Registration is limited to @ndmu.edu.ph institutional emails.", "error")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        else:
            db = get_db()
            try:
                cur = db.execute(
                    "INSERT INTO users (email,password_hash,created_at) VALUES (?,?,?)",
                    (email, generate_password_hash(password), datetime.now(PHILIPPINES).isoformat())
                )
                uid = cur.lastrowid
                db.commit()
            except sqlite3.IntegrityError:
                db.close()
                flash("That institutional email already has an account.", "error")
                return render_template("register.html")
            db.close()
            try:
                claim_legacy_data(uid)
            except sqlite3.Error:
                # The account itself is valid even if a legacy migration needs manual cleanup.
                ensure_user_settings(uid)
            session.clear()
            session["user_id"] = uid
            return redirect(url_for("home"))
    return render_template("register.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def home():
    uid = current_user_id()
    now = datetime.now(PHILIPPINES)
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    db = get_db()
    classes = db.execute(
        "SELECT * FROM classes WHERE user_id=? ORDER BY day_of_week,start_time", (uid,)
    ).fetchall()
    db.close()
    schedule = {d: [dict(c) for c in classes if c["day_of_week"] == i] for i, d in enumerate(days)}
    return render_template(
        "index.html",
        schedule=schedule,
        days=days,
        next_class=get_next_class(uid),
        today_name=days[now.weekday()],
        current_time=now.strftime("%H:%M:%S"),
        user=get_current_user()
    )


@app.route("/settings")
@login_required
def settings():
    uid = current_user_id()
    ensure_user_settings(uid)
    db = get_db()
    settings = db.execute("SELECT * FROM notification_settings WHERE user_id=?", (uid,)).fetchone()
    history = db.execute(
        "SELECT s.class_date,s.sent_at,c.subject FROM sent_reminders s "
        "JOIN classes c ON c.id=s.class_id WHERE c.user_id=? ORDER BY s.sent_at DESC LIMIT 20",
        (uid,)
    ).fetchall()
    db.close()
    return render_template("settings.html", settings=settings, notification_history=history, user=get_current_user())


@app.route("/settings/save", methods=["POST"])
@login_required
def save_settings():
    email = request.form.get("email", "").strip().lower()
    if not valid_ndmu_email(email):
        flash("Notification email must be an @ndmu.edu.ph address.", "error")
        return redirect(url_for("settings"))
    try:
        minutes = max(1, min(int(request.form.get("minutes_before", 30)), 1440))
    except ValueError:
        minutes = 30
    enabled = 1 if request.form.get("enabled") == "on" else 0
    ensure_user_settings(current_user_id())
    db = get_db()
    db.execute(
        "UPDATE notification_settings SET enabled=?,email=?,minutes_before=? WHERE user_id=?",
        (enabled, email, minutes, current_user_id())
    )
    db.commit()
    db.close()
    flash("Notification settings saved.", "success")
    return redirect(url_for("settings"))


@app.route("/add", methods=["POST"])
@login_required
def add_class():
    days = request.form.getlist("days_of_week")
    subject = request.form.get("subject", "").strip()
    start = request.form.get("start_time", "")
    end = request.form.get("end_time", "")
    room = request.form.get("room", "").strip() or "NOT SET"
    if not days or not subject or not start or not end:
        flash("Please select at least one day and complete the class details.", "error")
        return redirect(url_for("home") + "#add")
    if end <= start:
        flash("End time must be later than start time.", "error")
        return redirect(url_for("home") + "#add")
    db = get_db()
    for day in days:
        try:
            day = int(day)
            if 0 <= day <= 6:
                db.execute(
                    "INSERT INTO classes (user_id,day_of_week,subject,start_time,end_time,room) VALUES (?,?,?,?,?,?)",
                    (current_user_id(), day, subject, start, end, room)
                )
        except ValueError:
            pass
    db.commit()
    db.close()
    return redirect(url_for("home"))


@app.route("/delete/<int:class_id>", methods=["POST"])
@login_required
def delete_class(class_id):
    db = get_db()
    db.execute("DELETE FROM classes WHERE id=? AND user_id=?", (class_id, current_user_id()))
    db.commit()
    db.close()
    return redirect(url_for("home"))


@app.route("/edit/<int:class_id>", methods=["GET", "POST"])
@login_required
def edit_class(class_id):
    db = get_db()
    c = db.execute(
        "SELECT * FROM classes WHERE id=? AND user_id=?", (class_id, current_user_id())
    ).fetchone()
    if not c:
        db.close()
        return redirect(url_for("home"))
    if request.method == "POST":
        try:
            day = int(request.form["day_of_week"])
        except ValueError:
            day = -1
        subject = request.form.get("subject", "").strip()
        start = request.form.get("start_time", "")
        end = request.form.get("end_time", "")
        room = request.form.get("room", "").strip() or "NOT SET"
        if day not in range(7) or not subject or end <= start:
            db.close()
            flash("Please enter valid class details.", "error")
            return redirect(url_for("edit_class", class_id=class_id))
        db.execute(
            "UPDATE classes SET day_of_week=?,subject=?,start_time=?,end_time=?,room=? WHERE id=? AND user_id=?",
            (day, subject, start, end, room, class_id, current_user_id())
        )
        db.commit()
        db.close()
        return redirect(url_for("home"))
    db.close()
    return render_template("edit.html", class_item=c, user=get_current_user())


@app.route("/import-schedule")
@login_required
def import_schedule():
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    return render_template("import.html", days=days, user=get_current_user())


@app.route("/api/classes/import", methods=["POST"])
@login_required
def import_classes_api():
    payload = request.get_json(silent=True) or {}
    items = payload.get("classes")
    if not isinstance(items, list) or not items:
        return jsonify({"ok": False, "error": "No classes were provided."}), 400
    if len(items) > 100:
        return jsonify({"ok": False, "error": "You can import up to 100 classes at once."}), 400

    cleaned = []
    seen = set()
    for raw in items:
        try:
            day = int(raw.get("day_of_week"))
        except (TypeError, ValueError, AttributeError):
            continue
        subject = str(raw.get("subject", "")).strip()[:120]
        start = str(raw.get("start_time", "")).strip()
        end = str(raw.get("end_time", "")).strip()
        room = str(raw.get("room", "")).strip()[:120] or "NOT SET"
        if day not in range(7) or not subject:
            continue
        try:
            datetime.strptime(start, "%H:%M")
            datetime.strptime(end, "%H:%M")
        except ValueError:
            continue
        if end <= start:
            continue
        key = (day, subject.casefold(), start, end, room.casefold())
        if key in seen:
            continue
        seen.add(key)
        cleaned.append((day, subject, start, end, room))

    if not cleaned:
        return jsonify({"ok": False, "error": "No valid class rows were found."}), 400

    db = get_db()
    added = 0
    skipped = 0
    for day, subject, start, end, room in cleaned:
        duplicate = db.execute(
            "SELECT id FROM classes WHERE user_id=? AND day_of_week=? AND subject=? AND start_time=? AND end_time=? AND room=? LIMIT 1",
            (current_user_id(), day, subject, start, end, room)
        ).fetchone()
        if duplicate:
            skipped += 1
            continue
        db.execute(
            "INSERT INTO classes (user_id,day_of_week,subject,start_time,end_time,room) VALUES (?,?,?,?,?,?)",
            (current_user_id(), day, subject, start, end, room)
        )
        added += 1
    db.commit()
    db.close()
    return jsonify({"ok": True, "added": added, "skipped": skipped})


@app.route("/test-email", methods=["POST"])
@login_required
def test_email():
    db = get_db()
    s = db.execute("SELECT * FROM notification_settings WHERE user_id=?", (current_user_id(),)).fetchone()
    db.close()
    if not s or not s["enabled"]:
        flash("Enable email notifications first.", "error")
        return redirect(url_for("settings"))
    if not valid_ndmu_email(s["email"]):
        flash("Set a valid @ndmu.edu.ph notification email first.", "error")
        return redirect(url_for("settings"))
    ok = send_email(
        s["email"],
        "📚 Class Schedule App - Test Email",
        "Hello!\n\nThis is a test email from your Class Schedule App.\n\nYour institutional email notifications are working correctly.\n\n— Class Schedule App"
    )
    flash(
        "Test email sent successfully!" if ok else "Failed to send test email. Check the server terminal.",
        "success" if ok else "error"
    )
    return redirect(url_for("settings"))


if __name__ == "__main__":
    init_db()
    scheduler = BackgroundScheduler(timezone=PHILIPPINES)
    scheduler.add_job(check_for_reminders, "interval", minutes=1, id="class_reminder_checker", replace_existing=True)
    scheduler.start()
    print("Notification scheduler started.")
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)

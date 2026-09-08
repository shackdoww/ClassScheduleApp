from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3
import os
import smtplib
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
    return email.lower().strip().endswith(INSTITUTIONAL_DOMAIN)


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
    user_id = current_user_id()
    if not user_id:
        return None
    db = get_db()
    user = db.execute("SELECT id, email FROM users WHERE id = ?", (user_id,)).fetchone()
    db.close()
    return user


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

    # Upgrade databases created by the original single-user version.
    columns = [row["name"] for row in db.execute("PRAGMA table_info(classes)").fetchall()]
    if "user_id" not in columns:
        db.execute("ALTER TABLE classes ADD COLUMN user_id INTEGER")

    db.execute("""
        CREATE TABLE IF NOT EXISTS notification_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 0,
            email TEXT DEFAULT '',
            minutes_before INTEGER NOT NULL DEFAULT 30
        )
    """)

    settings_columns = [row["name"] for row in db.execute("PRAGMA table_info(notification_settings)").fetchall()]
    if "user_id" not in settings_columns:
        db.execute("ALTER TABLE notification_settings ADD COLUMN user_id INTEGER")

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

    # Keep the original starter timetable, but only create it on a completely
    # fresh database. It is assigned to the first account when that account registers.
    if user_count == 0 and class_count == 0:
        add_initial_schedule(db)

    db.commit()
    db.close()


def add_initial_schedule(db):
    classes = [
        (0, "SSP 114", "10:30", "11:30", "DOHERTY V310"),
        (0, "CSPC 103", "13:30", "16:00", "3013A"),
        (0, "ENGL 103", "16:30", "17:30", "DOHERTY V122"),
        (1, "MST 112", "07:30", "09:00", "DOHERTY V218"),
        (1, "RE 113", "09:00", "10:30", "CREEGAN 14"),
        (1, "CSCC 104", "13:00", "15:30", "NOT SET"),
        (1, "CSMATH 3", "17:30", "19:00", "NOT SET"),
        (2, "SSP 114", "10:30", "11:30", "DOHERTY V310"),
        (2, "CSPC 103", "13:30", "16:00", "3013A"),
        (2, "ENGL 103", "16:30", "17:30", "DOHERTY V122"),
        (3, "MST 112", "07:30", "09:00", "DOHERTY V218"),
        (3, "RE 113", "09:00", "10:30", "CREEGAN 14"),
        (3, "CSCC 104", "13:00", "15:30", "NOT SET"),
        (3, "CSMATH 3", "17:30", "19:00", "NOT SET"),
        (4, "PE 3", "08:30", "10:30", "GYM 1"),
        (4, "SSP 114", "10:30", "11:30", "DOHERTY V310"),
        (4, "CSPC 102", "13:30", "16:00", "3017A"),
        (4, "ENGL 103", "16:30", "17:30", "DOHERTY V122"),
    ]
    db.executemany("""
        INSERT INTO classes (day_of_week, subject, start_time, end_time, room)
        VALUES (?, ?, ?, ?, ?)
    """, classes)


def claim_legacy_data(user_id):
    db = get_db()
    # The original app had one global timetable. Give it to the first account.
    has_owned_classes = db.execute("SELECT 1 FROM classes WHERE user_id = ? LIMIT 1", (user_id,)).fetchone()
    if not has_owned_classes:
        db.execute("UPDATE classes SET user_id = ? WHERE user_id IS NULL", (user_id,))
    db.execute("UPDATE notification_settings SET user_id = ? WHERE user_id IS NULL", (user_id,))
    existing = db.execute("SELECT 1 FROM notification_settings WHERE user_id = ?", (user_id,)).fetchone()
    if not existing:
        user = db.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
        db.execute("INSERT INTO notification_settings (user_id, enabled, email, minutes_before) VALUES (?, 0, ?, 30)", (user_id, user["email"]))
    db.commit()
    db.close()


def ensure_user_settings(user_id):
    db = get_db()
    row = db.execute("SELECT 1 FROM notification_settings WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        user = db.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()
        db.execute("INSERT INTO notification_settings (user_id, enabled, email, minutes_before) VALUES (?, 0, ?, 30)", (user_id, user["email"]))
        db.commit()
    db.close()


def get_today_classes(user_id):
    db = get_db()
    classes = db.execute("""
        SELECT * FROM classes
        WHERE user_id = ? AND day_of_week = ?
        ORDER BY start_time
    """, (user_id, datetime.now(PHILIPPINES).weekday())).fetchall()
    db.close()
    return classes


def get_next_class(user_id):
    now = datetime.now(PHILIPPINES)
    db = get_db()
    classes = db.execute("""
        SELECT * FROM classes
        WHERE user_id = ?
        ORDER BY day_of_week, start_time
    """, (user_id,)).fetchall()
    db.close()

    candidates = []
    for class_item in classes:
        days_ahead = (class_item["day_of_week"] - now.weekday()) % 7
        class_date = now.date() + timedelta(days=days_ahead)
        start_time = datetime.strptime(class_item["start_time"], "%H:%M").time()
        class_datetime = datetime.combine(class_date, start_time).replace(tzinfo=PHILIPPINES)
        if class_datetime > now:
            candidates.append((class_datetime, class_item))

    if not candidates:
        return None
    class_datetime, class_item = min(candidates, key=lambda x: x[0])
    return {"class": class_item, "datetime": class_datetime}


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
        print(f"Email sent successfully to {recipient}")
        return True
    except Exception as e:
        print(f"Email failed: {e}")
        return False


def check_for_reminders():
    now = datetime.now(PHILIPPINES)
    db = get_db()
    settings_rows = db.execute("SELECT * FROM notification_settings WHERE enabled = 1 AND email != ''").fetchall()

    for settings in settings_rows:
        user_id = settings["user_id"]
        recipient = settings["email"]
        minutes_before = settings["minutes_before"]

        for day_offset in range(2):
            class_date = now.date() + timedelta(days=day_offset)
            classes = db.execute("""
                SELECT * FROM classes
                WHERE user_id = ? AND day_of_week = ?
                ORDER BY start_time
            """, (user_id, class_date.weekday())).fetchall()

            for class_item in classes:
                start_time = datetime.strptime(class_item["start_time"], "%H:%M").time()
                class_start = datetime.combine(class_date, start_time).replace(tzinfo=PHILIPPINES)
                reminder_time = class_start - timedelta(minutes=minutes_before)
                seconds_until = (reminder_time - now).total_seconds()

                if seconds_until <= 0 and seconds_until > -90:
                    already_sent = db.execute("""
                        SELECT id FROM sent_reminders
                        WHERE class_id = ? AND class_date = ?
                    """, (class_item["id"], class_date.isoformat())).fetchone()
                    if already_sent:
                        continue

                    body = f"""Class Schedule Reminder\n\nYou have a class coming up.\n\nSubject: {class_item['subject']}\n\nTime: {class_item['start_time']} - {class_item['end_time']}\n\nRoom: {class_item['room']}\n\nThis class starts in approximately {minutes_before} minutes.\n\nHave a great class!"""
                    if send_email(recipient, f"Class Reminder: {class_item['subject']}", body):
                        try:
                            db.execute("""
                                INSERT INTO sent_reminders (class_id, class_date, sent_at)
                                VALUES (?, ?, ?)
                            """, (class_item["id"], class_date.isoformat(), now.isoformat()))
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
        if not valid_ndmu_email(email):
            flash("Use your institutional @ndmu.edu.ph email.", "error")
            return render_template("login.html")
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        db.close()
        if not user or not check_password_hash(user["password_hash"], password):
            flash("Incorrect institutional email or password.", "error")
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
                cursor = db.execute("""
                    INSERT INTO users (email, password_hash, created_at)
                    VALUES (?, ?, ?)
                """, (email, generate_password_hash(password), datetime.now(PHILIPPINES).isoformat()))
                user_id = cursor.lastrowid
                db.execute("INSERT INTO notification_settings (user_id, enabled, email, minutes_before) VALUES (?, 0, ?, 30)", (user_id, email))
                db.commit()
            except sqlite3.IntegrityError:
                db.close()
                flash("That institutional email already has an account.", "error")
                return render_template("register.html")
            db.close()

            # Preserve the original starter timetable for the first account.
            claim_legacy_data(user_id)
            session.clear()
            session["user_id"] = user_id
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
    user_id = current_user_id()
    now = datetime.now(PHILIPPINES)
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    db = get_db()
    all_classes = db.execute("""
        SELECT * FROM classes WHERE user_id = ?
        ORDER BY day_of_week, start_time
    """, (user_id,)).fetchall()
    db.close()

    schedule = {day: [dict(c) for c in all_classes if c["day_of_week"] == i] for i, day in enumerate(days)}
    return render_template(
        "index.html", schedule=schedule, days=days,
        today_classes=get_today_classes(user_id),
        next_class=get_next_class(user_id),
        today_name=days[now.weekday()],
        current_time=now.strftime("%H:%M:%S"),
        user=get_current_user()
    )


@app.route("/settings")
@login_required
def settings():
    user_id = current_user_id()
    ensure_user_settings(user_id)
    conn = get_db()
    settings = conn.execute("SELECT * FROM notification_settings WHERE user_id = ?", (user_id,)).fetchone()
    notification_history = conn.execute("""
        SELECT sent_reminders.class_date, sent_reminders.sent_at, classes.subject
        FROM sent_reminders
        JOIN classes ON classes.id = sent_reminders.class_id
        WHERE classes.user_id = ?
        ORDER BY sent_reminders.sent_at DESC
        LIMIT 20
    """, (user_id,)).fetchall()
    conn.close()
    return render_template("settings.html", settings=settings, notification_history=notification_history, user=get_current_user())


@app.route("/settings/save", methods=["POST"])
@login_required
def save_settings():
    email = request.form.get("email", "").strip().lower()
    if not valid_ndmu_email(email):
        flash("Notification email must be an @ndmu.edu.ph address.", "error")
        return redirect(url_for("settings"))

    try:
        minutes_before = int(request.form.get("minutes_before", 30))
    except ValueError:
        minutes_before = 30
    minutes_before = max(1, min(minutes_before, 1440))
    enabled = 1 if request.form.get("enabled") == "on" else 0

    db = get_db()
    db.execute("""
        UPDATE notification_settings
        SET enabled = ?, email = ?, minutes_before = ?
        WHERE user_id = ?
    """, (enabled, email, minutes_before, current_user_id()))
    db.commit()
    db.close()
    flash("Notification settings saved.", "success")
    return redirect(url_for("settings"))


@app.route("/add", methods=["POST"])
@login_required
def add_class():
    days = request.form.getlist("days_of_week")
    subject = request.form.get("subject", "").strip()
    start_time = request.form.get("start_time", "")
    end_time = request.form.get("end_time", "")
    room = request.form.get("room", "").strip() or "NOT SET"

    if not days or not subject or not start_time or not end_time:
        flash("Please complete the subject, days, and times.", "error")
        return redirect(url_for("home") + "#add")

    if end_time <= start_time:
        flash("End time must be later than start time.", "error")
        return redirect(url_for("home") + "#add")

    db = get_db()
    for day in days:
        try:
            day_number = int(day)
            if 0 <= day_number <= 6:
                db.execute("""
                    INSERT INTO classes (user_id, day_of_week, subject, start_time, end_time, room)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (current_user_id(), day_number, subject, start_time, end_time, room))
        except ValueError:
            continue
    db.commit()
    db.close()
    return redirect(url_for("home"))


@app.route("/delete/<int:class_id>", methods=["POST"])
@login_required
def delete_class(class_id):
    db = get_db()
    db.execute("DELETE FROM classes WHERE id = ? AND user_id = ?", (class_id, current_user_id()))
    db.commit()
    db.close()
    return redirect(url_for("home"))


@app.route("/edit/<int:class_id>", methods=["GET", "POST"])
@login_required
def edit_class(class_id):
    db = get_db()
    class_item = db.execute("SELECT * FROM classes WHERE id = ? AND user_id = ?", (class_id, current_user_id())).fetchone()
    if not class_item:
        db.close()
        return redirect(url_for("home"))

    if request.method == "POST":
        try:
            day = int(request.form["day_of_week"])
            if day < 0 or day > 6:
                raise ValueError
        except ValueError:
            db.close()
            flash("Invalid day.", "error")
            return redirect(url_for("edit_class", class_id=class_id))

        subject = request.form.get("subject", "").strip()
        start_time = request.form.get("start_time", "")
        end_time = request.form.get("end_time", "")
        room = request.form.get("room", "").strip() or "NOT SET"
        if not subject or end_time <= start_time:
            db.close()
            flash("Please enter valid class details.", "error")
            return redirect(url_for("edit_class", class_id=class_id))

        db.execute("""
            UPDATE classes SET day_of_week = ?, subject = ?, start_time = ?, end_time = ?, room = ?
            WHERE id = ? AND user_id = ?
        """, (day, subject, start_time, end_time, room, class_id, current_user_id()))
        db.commit()
        db.close()
        return redirect(url_for("home"))

    db.close()
    return render_template("edit.html", class_item=class_item, user=get_current_user())


@app.route("/test-email", methods=["POST"])
@login_required
def test_email():
    conn = get_db()
    settings = conn.execute("SELECT * FROM notification_settings WHERE user_id = ?", (current_user_id(),)).fetchone()
    conn.close()

    if not settings or not settings["enabled"]:
        flash("Enable email notifications first.", "error")
        return redirect(url_for("settings"))
    if not settings["email"] or not valid_ndmu_email(settings["email"]):
        flash("Set a valid @ndmu.edu.ph notification email first.", "error")
        return redirect(url_for("settings"))

    success = send_email(
        settings["email"],
        "📚 Class Schedule App - Test Email",
        "Hello!\n\nThis is a test email from your Class Schedule App.\n\nYour institutional email notifications are working correctly.\n\n— Class Schedule App"
    )
    flash("Test email sent successfully!" if success else "Failed to send test email. Check the server terminal.", "success" if success else "error")
    return redirect(url_for("settings"))


if __name__ == "__main__":
    init_db()
    scheduler = BackgroundScheduler(timezone=PHILIPPINES)
    scheduler.add_job(check_for_reminders, "interval", minutes=1, id="class_reminder_checker", replace_existing=True)
    scheduler.start()
    print("Notification scheduler started.")
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)

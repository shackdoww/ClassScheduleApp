from flask import Flask, render_template, request, redirect, url_for
import sqlite3
import os
import smtplib

from email.message import EmailMessage
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler


# ============================================================
# APP CONFIGURATION
# ============================================================

app = Flask(__name__)

DATABASE = "schedule.db"

# Philippines timezone
PHILIPPINES = ZoneInfo("Asia/Manila")


# Load values from .env
load_dotenv()

EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_APP_PASSWORD = os.getenv("EMAIL_APP_PASSWORD")


# ============================================================
# DATABASE
# ============================================================

def get_db():

    db = sqlite3.connect(DATABASE)

    db.row_factory = sqlite3.Row

    return db


def init_db():

    db = get_db()

    # --------------------------------------------------------
    # CLASSES TABLE
    # --------------------------------------------------------

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


    # --------------------------------------------------------
    # NOTIFICATION SETTINGS
    # --------------------------------------------------------

    db.execute("""
               CREATE TABLE IF NOT EXISTS notification_settings (

                                                                    id INTEGER PRIMARY KEY CHECK (id = 1),

                   enabled INTEGER NOT NULL DEFAULT 0,

                   email TEXT DEFAULT '',

                   minutes_before INTEGER NOT NULL DEFAULT 30

                   )
               """)


    # --------------------------------------------------------
    # REMINDER HISTORY
    #
    # Prevents the same reminder from being sent repeatedly.
    # --------------------------------------------------------

    db.execute("""
               CREATE TABLE IF NOT EXISTS sent_reminders (

                                                             id INTEGER PRIMARY KEY AUTOINCREMENT,

                                                             class_id INTEGER NOT NULL,

                                                             class_date TEXT NOT NULL,

                                                             sent_at TEXT NOT NULL,

                                                             UNIQUE(class_id, class_date)

                   )
               """)


    # --------------------------------------------------------
    # CREATE DEFAULT SETTINGS
    # --------------------------------------------------------

    settings = db.execute("""
                          SELECT *
                          FROM notification_settings
                          WHERE id = 1
                          """).fetchone()


    if settings is None:

        db.execute("""
                   INSERT INTO notification_settings
                   (
                       id,
                       enabled,
                       email,
                       minutes_before
                   )

                   VALUES
                       (
                           1,
                           0,
                           '',
                           30
                       )
                   """)


    # --------------------------------------------------------
    # INSERT INITIAL SCHEDULE IF EMPTY
    # --------------------------------------------------------

    count = db.execute(
        "SELECT COUNT(*) FROM classes"
    ).fetchone()[0]


    if count == 0:

        add_initial_schedule(db)


    db.commit()

    db.close()


# ============================================================
# INITIAL SCHEDULE
# ============================================================

def add_initial_schedule(db):

    classes = [

        # MONDAY
        (0, "SSP 114", "10:30", "11:30", "DOHERTY V310"),
        (0, "CSPC 103", "13:30", "16:00", "3013A"),
        (0, "ENGL 103", "16:30", "17:30", "DOHERTY V122"),

        # TUESDAY
        (1, "MST 112", "07:30", "09:00", "DOHERTY V218"),
        (1, "RE 113", "09:00", "10:30", "CREEGAN 14"),
        (1, "CSCC 104", "13:00", "15:30", "NOT SET"),
        (1, "CSMATH 3", "17:30", "19:00", "NOT SET"),

        # WEDNESDAY
        (2, "SSP 114", "10:30", "11:30", "DOHERTY V310"),
        (2, "CSPC 103", "13:30", "16:00", "3013A"),
        (2, "ENGL 103", "16:30", "17:30", "DOHERTY V122"),

        # THURSDAY
        (3, "MST 112", "07:30", "09:00", "DOHERTY V218"),
        (3, "RE 113", "09:00", "10:30", "CREEGAN 14"),
        (3, "CSCC 104", "13:00", "15:30", "NOT SET"),
        (3, "CSMATH 3", "17:30", "19:00", "NOT SET"),

        # FRIDAY
        (4, "PE 3", "08:30", "10:30", "GYM 1"),
        (4, "SSP 114", "10:30", "11:30", "DOHERTY V310"),
        (4, "CSPC 102", "13:30", "16:00", "3017A"),
        (4, "ENGL 103", "16:30", "17:30", "DOHERTY V122"),
    ]


    db.executemany("""
                   INSERT INTO classes
                   (
                       day_of_week,
                       subject,
                       start_time,
                       end_time,
                       room
                   )

                   VALUES (?, ?, ?, ?, ?)
                   """, classes)


    db.commit()


# ============================================================
# TODAY'S CLASSES
# ============================================================

def get_today_classes():

    now = datetime.now(PHILIPPINES)

    today = now.weekday()


    db = get_db()


    classes = db.execute("""
                         SELECT *
                         FROM classes

                         WHERE day_of_week = ?

                         ORDER BY start_time
                         """, (today,)).fetchall()


    db.close()


    return classes


# ============================================================
# FIND NEXT CLASS
# ============================================================

def get_next_class():

    now = datetime.now(PHILIPPINES)

    db = get_db()

    classes = db.execute("""
                         SELECT *
                         FROM classes
                         ORDER BY day_of_week, start_time
                         """).fetchall()

    db.close()

    today = now.weekday()

    candidates = []

    for class_item in classes:

        class_weekday = class_item["day_of_week"]

        # How many days from today until this class?
        days_ahead = (class_weekday - today) % 7

        # Date on which this class occurs
        class_date = (
                now.date()
                + timedelta(days=days_ahead)
        )

        # Convert start time into a time object
        start_time = datetime.strptime(
            class_item["start_time"],
            "%H:%M"
        ).time()

        # Combine date + start time
        class_datetime = datetime.combine(
            class_date,
            start_time
        ).replace(
            tzinfo=PHILIPPINES
        )

        # If it's today and the class has already started,
        # don't consider it the next class.
        if class_datetime <= now:
            continue

        candidates.append(
            (class_datetime, class_item)
        )

    if not candidates:
        return None

    # Find the closest upcoming class
    candidates.sort(
        key=lambda x: x[0]
    )

    class_datetime, class_item = candidates[0]

    return {
        "class": class_item,
        "datetime": class_datetime
    }


# ============================================================
# SEND EMAIL
# ============================================================

def send_email(recipient, subject, body):

    try:

        msg = EmailMessage()

        msg["From"] = EMAIL_ADDRESS
        msg["To"] = recipient
        msg["Subject"] = subject

        msg.set_content(body)

        with smtplib.SMTP("smtp.gmail.com", 587) as server:

            server.starttls()

            server.login(
                EMAIL_ADDRESS,
                EMAIL_APP_PASSWORD
            )

            server.send_message(msg)

        print(f"Email sent successfully to {recipient}")

        return True

    except Exception as e:

        print(f"Email failed: {e}")

        return False


# ============================================================
# CHECK UPCOMING CLASSES
# ============================================================

def check_for_reminders():

    now = datetime.now(PHILIPPINES)

    print(
        f"[Notification check] "
        f"{now.strftime('%Y-%m-%d %H:%M:%S')}"
    )


    db = get_db()


    # --------------------------------------------------------
    # GET SETTINGS
    # --------------------------------------------------------

    settings = db.execute("""
                          SELECT *
                          FROM notification_settings

                          WHERE id = 1
                          """).fetchone()


    if settings is None:

        db.close()

        return


    # Notifications disabled
    if not settings["enabled"]:

        db.close()

        return


    recipient = settings["email"]

    minutes_before = settings["minutes_before"]


    # No recipient configured
    if not recipient:

        print(
            "Notifications enabled, "
            "but no recipient email is configured."
        )

        db.close()

        return


    # --------------------------------------------------------
    # CHECK TODAY AND TOMORROW
    # --------------------------------------------------------

    for day_offset in range(0, 2):

        class_date = (
                now.date()
                + timedelta(days=day_offset)
        )


        weekday = class_date.weekday()


        classes = db.execute("""
                             SELECT *
                             FROM classes

                             WHERE day_of_week = ?

                             ORDER BY start_time
                             """, (weekday,)).fetchall()


        for class_item in classes:

            start_time = datetime.strptime(
                class_item["start_time"],
                "%H:%M"
            ).time()


            class_start = datetime.combine(
                class_date,
                start_time
            ).replace(
                tzinfo=PHILIPPINES
            )


            # ------------------------------------------------
            # Calculate reminder time
            # ------------------------------------------------

            reminder_time = (
                    class_start
                    - timedelta(
                minutes=minutes_before
            )
            )


            # ------------------------------------------------
            # Is it time to send?
            #
            # Give the scheduler a small window because it
            # checks once per minute.
            # ------------------------------------------------

            seconds_until_reminder = (
                    reminder_time - now
            ).total_seconds()


            if (
                    seconds_until_reminder <= 0
                    and seconds_until_reminder > -90
            ):

                # --------------------------------------------
                # Check whether reminder was already sent
                # --------------------------------------------

                already_sent = db.execute("""
                                          SELECT id

                                          FROM sent_reminders

                                          WHERE class_id = ?

                                            AND class_date = ?
                                          """, (
                                              class_item["id"],
                                              class_date.isoformat()
                                          )).fetchone()


                if already_sent:

                    continue


                # --------------------------------------------
                # Email contents
                # --------------------------------------------

                subject = (
                    f"Class Reminder: "
                    f"{class_item['subject']}"
                )


                body = f"""
Class Schedule Reminder

You have a class coming up.

Subject: {class_item['subject']}

Time:
{class_item['start_time']} - {class_item['end_time']}

Room:
{class_item['room']}

This class starts in approximately
{minutes_before} minutes.

Have a great class!
"""


                # --------------------------------------------
                # Send email
                # --------------------------------------------

                success = send_email(
                    recipient,
                    subject,
                    body
                )


                # --------------------------------------------
                # Record successful reminder
                # --------------------------------------------

                if success:

                    try:

                        db.execute("""
                                   INSERT INTO sent_reminders
                                   (
                                       class_id,
                                       class_date,
                                       sent_at
                                   )

                                   VALUES (?, ?, ?)
                                   """, (
                                       class_item["id"],
                                       class_date.isoformat(),
                                       now.isoformat()
                                   ))

                        db.commit()


                    except sqlite3.IntegrityError:

                        # Another scheduler check may have
                        # already inserted it.
                        pass


    db.close()


# ============================================================
# HOME / DASHBOARD
# ============================================================

@app.route("/")
def home():

    now = datetime.now(PHILIPPINES)

    today_classes = get_today_classes()

    next_class = get_next_class()


    days = [
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday"
    ]


    today_name = days[
        now.weekday()
    ]


    db = get_db()


    all_classes = db.execute("""
                             SELECT *
                             FROM classes

                             ORDER BY
                                 day_of_week,
                                 start_time
                             """).fetchall()


    db.close()


    schedule = {}


    for number, day in enumerate(days):

        schedule[day] = [

            dict(class_item)

            for class_item in all_classes

            if class_item["day_of_week"] == number

        ]


    return render_template(
        "index.html",

        schedule=schedule,

        days=days,

        today_classes=today_classes,

        next_class=next_class,

        today_name=today_name,

        current_time=now.strftime(
            "%H:%M:%S"
        )
    )


# ============================================================
# NOTIFICATION SETTINGS
# ============================================================

@app.route("/settings")
def settings():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row

    settings = conn.execute(
        "SELECT * FROM notification_settings WHERE id = 1"
    ).fetchone()

    notification_history = conn.execute("""
                                        SELECT
                                            sent_reminders.class_date,
                                            sent_reminders.sent_at,
                                            classes.subject
                                        FROM sent_reminders
                                                 JOIN classes
                                                      ON classes.id = sent_reminders.class_id
                                        ORDER BY sent_reminders.sent_at DESC
                                            LIMIT 20
                                        """).fetchall()

    conn.close()

    notification_history = [
        {
            "class_date": row["class_date"],
            "sent_at": row["sent_at"],
            "subject": row["subject"]
        }
        for row in notification_history
    ]

    return render_template(
        "settings.html",
        settings=settings,
        notification_history=notification_history
    )


# ============================================================
# SAVE NOTIFICATION SETTINGS
# ============================================================

@app.route(
    "/settings/save",
    methods=["POST"]
)
def save_settings():

    enabled = (
        1
        if request.form.get("enabled") == "on"
        else 0
    )


    email = request.form.get(
        "email",
        ""
    ).strip()


    minutes_before = int(
        request.form.get(
            "minutes_before",
            30
        )
    )


    db = get_db()


    db.execute("""
               UPDATE notification_settings

               SET
                   enabled = ?,
                   email = ?,
                   minutes_before = ?

               WHERE id = 1
               """, (
                   enabled,
                   email,
                   minutes_before
               ))


    db.commit()

    db.close()


    return redirect(
        url_for("settings")
    )


# ============================================================
# ADD CLASS
# ============================================================

@app.route(
    "/add",
    methods=["POST"]
)
def add_class():

    day = int(
        request.form["day_of_week"]
    )


    subject = request.form[
        "subject"
    ].strip()


    start_time = request.form[
        "start_time"
    ]


    end_time = request.form[
        "end_time"
    ]


    room = request.form[
        "room"
    ].strip()


    db = get_db()


    db.execute("""
               INSERT INTO classes
               (
                   day_of_week,
                   subject,
                   start_time,
                   end_time,
                   room
               )

               VALUES (?, ?, ?, ?, ?)
               """, (
                   day,
                   subject,
                   start_time,
                   end_time,
                   room
               ))


    db.commit()

    db.close()


    return redirect(
        url_for("home")
    )


# ============================================================
# DELETE CLASS
# ============================================================

@app.route(
    "/delete/<int:class_id>",
    methods=["POST"]
)
def delete_class(class_id):

    db = get_db()


    db.execute(
        """
        DELETE FROM classes

        WHERE id = ?
        """,
        (class_id,)
    )


    db.commit()

    db.close()


    return redirect(
        url_for("home")
    )


# ============================================================
# EDIT CLASS
# ============================================================

@app.route(
    "/edit/<int:class_id>",
    methods=["GET", "POST"]
)
def edit_class(class_id):

    db = get_db()


    # --------------------------------------------------------
    # SAVE CHANGES
    # --------------------------------------------------------

    if request.method == "POST":

        day = int(
            request.form["day_of_week"]
        )


        subject = request.form[
            "subject"
        ].strip()


        start_time = request.form[
            "start_time"
        ]


        end_time = request.form[
            "end_time"
        ]


        room = request.form[
            "room"
        ].strip()


        db.execute("""
                   UPDATE classes

                   SET
                       day_of_week = ?,
                       subject = ?,
                       start_time = ?,
                       end_time = ?,
                       room = ?

                   WHERE id = ?
                   """, (
                       day,
                       subject,
                       start_time,
                       end_time,
                       room,
                       class_id
                   ))


        db.commit()

        db.close()


        return redirect(
            url_for("home")
        )


    # --------------------------------------------------------
    # GET CLASS
    # --------------------------------------------------------

    class_item = db.execute(
        """
        SELECT *
        FROM classes

        WHERE id = ?
        """,
        (class_id,)
    ).fetchone()


    db.close()


    return render_template(
        "edit.html",
        class_item=class_item
    )


# ============================================================
# START APPLICATION
# ============================================================
@app.route("/test-email", methods=["POST"])
def test_email():

    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row

    settings = conn.execute(
        "SELECT * FROM notification_settings WHERE id = 1"
    ).fetchone()

    conn.close()

    if not settings["enabled"]:
        return """
        <script>
            alert("Email notifications are currently disabled.");
            window.location.href = "/settings";
        </script>
        """

    recipient = settings["email"]

    if not recipient:
        return """
        <script>
            alert("Please enter an email address first.");
            window.location.href = "/settings";
        </script>
        """

    subject = "📚 Class Schedule App - Test Email"

    body = """\
Hello!

This is a test email from your Class Schedule App.

If you received this message, your email notifications are working correctly.

You can now receive class reminders.

— Class Schedule App
"""

    success = send_email(
        recipient,
        subject,
        body
    )

    if success:
        message = "Test email sent successfully!"
    else:
        message = "Failed to send test email. Check the terminal for errors."

    return f"""
    <script>
        alert("{message}");
        window.location.href = "/settings";
    </script>
    """

if __name__ == "__main__":

    # Create database/tables
    init_db()


    # --------------------------------------------------------
    # START NOTIFICATION SCHEDULER
    # --------------------------------------------------------

    scheduler = BackgroundScheduler(
        timezone=PHILIPPINES
    )


    scheduler.add_job(
        check_for_reminders,
        "interval",
        minutes=1,
        id="class_reminder_checker",
        replace_existing=True
    )


    scheduler.start()


    print(
        "Notification scheduler started."
    )


    # --------------------------------------------------------
    # START FLASK
    # --------------------------------------------------------

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True,
        use_reloader=False
    )
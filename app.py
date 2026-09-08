from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, Response
import sqlite3, os, smtplib, secrets
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

DAYS = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]

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

def table_columns(db, table):
    return [r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]

def add_column(db, table, name, spec):
    if name not in table_columns(db, table):
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")

def migrate_notification_settings(db):
    table = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='notification_settings'").fetchone()
    new_schema = """CREATE TABLE notification_settings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER UNIQUE NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 0,
        email TEXT DEFAULT '',
        minutes_before INTEGER NOT NULL DEFAULT 30,
        second_minutes_before INTEGER NOT NULL DEFAULT 0,
        class_start_enabled INTEGER NOT NULL DEFAULT 0,
        morning_digest_enabled INTEGER NOT NULL DEFAULT 0,
        commute_minutes INTEGER NOT NULL DEFAULT 0
    )"""
    if not table:
        db.execute(new_schema); return
    cols = table_columns(db, "notification_settings")
    if "user_id" not in cols:
        legacy_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_settings_legacy'").fetchone()
        if legacy_exists: db.execute("DROP TABLE notification_settings_legacy")
        db.execute("ALTER TABLE notification_settings RENAME TO notification_settings_legacy")
        db.execute(new_schema)
    else:
        add_column(db, "notification_settings", "second_minutes_before", "INTEGER NOT NULL DEFAULT 0")
        add_column(db, "notification_settings", "class_start_enabled", "INTEGER NOT NULL DEFAULT 0")
        add_column(db, "notification_settings", "morning_digest_enabled", "INTEGER NOT NULL DEFAULT 0")
        add_column(db, "notification_settings", "commute_minutes", "INTEGER NOT NULL DEFAULT 0")

def init_db():
    db = get_db()
    db.execute("""CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        share_token TEXT UNIQUE)""")
    db.execute("""CREATE TABLE IF NOT EXISTS classes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day_of_week INTEGER NOT NULL,
        subject TEXT NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        room TEXT)""")
    for name, spec in [
        ("user_id","INTEGER"),("teacher","TEXT DEFAULT ''"),("notes","TEXT DEFAULT ''"),
        ("color","TEXT DEFAULT '#8b7cff'"),("mode","TEXT DEFAULT 'On campus'"),
        ("meeting_url","TEXT DEFAULT ''"),("course_type","TEXT DEFAULT 'Class'"),
    ]:
        add_column(db, "classes", name, spec)
    migrate_notification_settings(db)
    db.execute("""CREATE TABLE IF NOT EXISTS sent_reminders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        class_id INTEGER NOT NULL,
        class_date TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        UNIQUE(class_id,class_date))""")
    db.execute("""CREATE TABLE IF NOT EXISTS semesters(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        start_date TEXT,
        end_date TEXT,
        is_current INTEGER DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        class_id INTEGER,
        title TEXT NOT NULL,
        description TEXT DEFAULT '',
        due_at TEXT,
        priority INTEGER DEFAULT 1,
        kind TEXT DEFAULT 'task',
        done INTEGER DEFAULT 0,
        starred INTEGER DEFAULT 0,
        created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS holidays(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS personal_events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        start_at TEXT NOT NULL,
        end_at TEXT,
        location TEXT DEFAULT '',
        notes TEXT DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS attendance(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        class_id INTEGER NOT NULL,
        class_date TEXT NOT NULL,
        status TEXT NOT NULL,
        UNIQUE(user_id,class_id,class_date))""")
    db.execute("""CREATE TABLE IF NOT EXISTS grades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        class_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        score REAL NOT NULL,
        max_score REAL NOT NULL,
        weight REAL DEFAULT 0,
        created_at TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(user_id,done,due_at)")
    user_count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    class_count = db.execute("SELECT COUNT(*) FROM classes").fetchone()[0]
    if user_count == 0 and class_count == 0:
        add_initial_schedule(db)
    db.commit(); db.close()

def add_initial_schedule(db):
    classes = [
        (0,"SSP 114","10:30","11:30","DOHERTY V310"),(0,"CSPC 103","13:30","16:00","3013A"),(0,"ENGL 103","16:30","17:30","DOHERTY V122"),
        (1,"MST 112","07:30","09:00","DOHERTY V218"),(1,"RE 113","09:00","10:30","CREEGAN 14"),(1,"CSCC 104","13:00","15:30","NOT SET"),(1,"CSMATH 3","17:30","19:00","NOT SET"),
        (2,"SSP 114","10:30","11:30","DOHERTY V310"),(2,"CSPC 103","13:30","16:00","3013A"),(2,"ENGL 103","16:30","17:30","DOHERTY V122"),
        (3,"MST 112","07:30","09:00","DOHERTY V218"),(3,"RE 113","09:00","10:30","CREEGAN 14"),(3,"CSCC 104","13:00","15:30","NOT SET"),(3,"CSMATH 3","17:30","19:00","NOT SET"),
        (4,"PE 3","08:30","10:30","GYM 1"),(4,"SSP 114","10:30","11:30","DOHERTY V310"),(4,"CSPC 102","13:30","16:00","3017A"),(4,"ENGL 103","16:30","17:30","DOHERTY V122")]
    db.executemany("INSERT INTO classes(day_of_week,subject,start_time,end_time,room) VALUES(?,?,?,?,?)", classes)

def claim_legacy_data(uid):
    db=get_db()
    if not db.execute("SELECT 1 FROM classes WHERE user_id=? LIMIT 1",(uid,)).fetchone():
        db.execute("UPDATE classes SET user_id=? WHERE user_id IS NULL",(uid,))
    legacy=db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='notification_settings_legacy'").fetchone()
    user=db.execute("SELECT email FROM users WHERE id=?",(uid,)).fetchone()
    if legacy and user:
        old=db.execute("SELECT enabled,email,minutes_before FROM notification_settings_legacy LIMIT 1").fetchone()
        if old:
            email=(old["email"] or "").strip().lower()
            if not valid_ndmu_email(email): email=user["email"]
            db.execute("INSERT OR IGNORE INTO notification_settings(user_id,enabled,email,minutes_before) VALUES(?,?,?,?)",(uid,old["enabled"],email,old["minutes_before"]))
        db.execute("DROP TABLE notification_settings_legacy")
    db.execute("UPDATE users SET share_token=COALESCE(share_token,?) WHERE id=?",(secrets.token_urlsafe(18),uid))
    db.commit(); db.close(); ensure_user_settings(uid)

def ensure_user_settings(uid):
    db=get_db(); user=db.execute("SELECT email FROM users WHERE id=?",(uid,)).fetchone()
    if user:
        db.execute("INSERT OR IGNORE INTO notification_settings(user_id,enabled,email,minutes_before,second_minutes_before,class_start_enabled,morning_digest_enabled,commute_minutes) VALUES(?,0,?,30,0,0,0,0)",(uid,user["email"]))
    db.commit(); db.close()

def is_holiday(uid,d):
    db=get_db(); r=db.execute("SELECT 1 FROM holidays WHERE user_id=? AND start_date<=? AND end_date>=?",(uid,d.isoformat(),d.isoformat())).fetchone(); db.close(); return bool(r)

def class_rows(uid):
    db=get_db(); r=db.execute("SELECT * FROM classes WHERE user_id=? ORDER BY day_of_week,start_time",(uid,)).fetchall(); db.close(); return r

def get_next_class(uid):
    now=datetime.now(PHILIPPINES); candidates=[]
    for c in class_rows(uid):
        d=now.date()+timedelta(days=(c["day_of_week"]-now.weekday())%7)
        dt=datetime.combine(d,datetime.strptime(c["start_time"],"%H:%M").time()).replace(tzinfo=PHILIPPINES)
        if dt>now and not is_holiday(uid,d): candidates.append((dt,c))
    if not candidates: return None
    dt,c=min(candidates,key=lambda x:x[0]); return {"class":c,"datetime":dt}

def send_email(recipient,subject,body):
    if not EMAIL_ADDRESS or not EMAIL_APP_PASSWORD: return False
    try:
        msg=EmailMessage(); msg["From"]=EMAIL_ADDRESS; msg["To"]=recipient; msg["Subject"]=subject; msg.set_content(body)
        with smtplib.SMTP("smtp.gmail.com",587) as server:
            server.starttls(); server.login(EMAIL_ADDRESS,EMAIL_APP_PASSWORD); server.send_message(msg)
        return True
    except Exception as e:
        print(f"Email failed: {e}"); return False

def check_for_reminders():
    now=datetime.now(PHILIPPINES); db=get_db(); settings_rows=db.execute("SELECT * FROM notification_settings WHERE enabled=1 AND email!=''").fetchall()
    for s in settings_rows:
        for offset in range(2):
            d=now.date()+timedelta(days=offset)
            if is_holiday(s["user_id"],d): continue
            classes=db.execute("SELECT * FROM classes WHERE user_id=? AND day_of_week=? ORDER BY start_time",(s["user_id"],d.weekday())).fetchall()
            for c in classes:
                dt=datetime.combine(d,datetime.strptime(c["start_time"],"%H:%M").time()).replace(tzinfo=PHILIPPINES)
                for mins in {s["minutes_before"],s["second_minutes_before"]}:
                    if not mins: continue
                    reminder=dt-timedelta(minutes=mins); key=f"{d.isoformat()}:{mins}"
                    already=db.execute("SELECT 1 FROM sent_reminders WHERE class_id=? AND class_date=?",(c["id"],key)).fetchone()
                    if -90<(reminder-now).total_seconds()<=0 and not already:
                        body=f"Class Schedule Reminder\n\n{c['subject']}\n{c['start_time']} - {c['end_time']}\nRoom: {c['room']}\nTeacher: {c['teacher'] or 'Not set'}\nStarts in about {mins} minutes."
                        if send_email(s["email"],f"Class in {mins} min: {c['subject']}",body):
                            try: db.execute("INSERT INTO sent_reminders(class_id,class_date,sent_at) VALUES(?,?,?)",(c["id"],key,now.isoformat())); db.commit()
                            except sqlite3.IntegrityError: pass
                if s["class_start_enabled"]:
                    key=f"{d.isoformat()}:start"; already=db.execute("SELECT 1 FROM sent_reminders WHERE class_id=? AND class_date=?",(c["id"],key)).fetchone()
                    if -90<(dt-now).total_seconds()<=0 and not already and send_email(s["email"],f"Starting now: {c['subject']}",f"Your {c['subject']} class is starting now.\nRoom: {c['room']}"):
                        try: db.execute("INSERT INTO sent_reminders(class_id,class_date,sent_at) VALUES(?,?,?)",(c["id"],key,now.isoformat())); db.commit()
                        except sqlite3.IntegrityError: pass
    db.close()

@app.route("/login",methods=["GET","POST"])
def login():
    if current_user_id(): return redirect(url_for("home"))
    if request.method=="POST":
        email=request.form.get("email","").strip().lower(); password=request.form.get("password",""); db=get_db(); u=db.execute("SELECT * FROM users WHERE email=?",(email,)).fetchone(); db.close()
        if not valid_ndmu_email(email) or not u or not check_password_hash(u["password_hash"],password): flash("Use a valid @ndmu.edu.ph account and password.","error")
        else: session.clear(); session["user_id"]=u["id"]; ensure_user_settings(u["id"]); return redirect(url_for("home"))
    return render_template("login.html")

@app.route("/register",methods=["GET","POST"])
def register():
    if current_user_id(): return redirect(url_for("home"))
    if request.method=="POST":
        email=request.form.get("email","").strip().lower(); password=request.form.get("password",""); confirm=request.form.get("confirm_password","")
        if not valid_ndmu_email(email): flash("Registration is limited to @ndmu.edu.ph institutional emails.","error")
        elif len(password)<8: flash("Password must be at least 8 characters.","error")
        elif password!=confirm: flash("Passwords do not match.","error")
        else:
            db=get_db()
            try:
                cur=db.execute("INSERT INTO users(email,password_hash,created_at,share_token) VALUES(?,?,?,?)",(email,generate_password_hash(password),datetime.now(PHILIPPINES).isoformat(),secrets.token_urlsafe(18))); new_uid=cur.lastrowid; db.commit()
            except sqlite3.IntegrityError: db.close(); flash("That institutional email already has an account.","error"); return render_template("register.html")
            db.close(); claim_legacy_data(new_uid); session.clear(); session["user_id"]=new_uid; return redirect(url_for("home"))
    return render_template("register.html")

@app.post("/logout")
@login_required
def logout(): session.clear(); return redirect(url_for("login"))

@app.route("/")
@login_required
def home():
    uid=current_user_id(); now=datetime.now(PHILIPPINES); rows=class_rows(uid); sched={d:[dict(c) for c in rows if c["day_of_week"]==i] for i,d in enumerate(DAYS)}
    db=get_db(); tasks=db.execute("SELECT t.*,c.subject FROM tasks t LEFT JOIN classes c ON c.id=t.class_id WHERE t.user_id=? AND t.done=0 ORDER BY t.starred DESC,CASE WHEN t.due_at IS NULL THEN 1 ELSE 0 END,t.due_at LIMIT 5",(uid,)).fetchall(); db.close()
    return render_template("index.html",schedule=sched,days=DAYS,next_class=get_next_class(uid),today_name=DAYS[now.weekday()],current_time=now.strftime("%H:%M:%S"),user=get_current_user(),tasks=tasks)

@app.route("/settings")
@login_required
def settings():
    uid=current_user_id(); ensure_user_settings(uid); db=get_db(); settings=db.execute("SELECT * FROM notification_settings WHERE user_id=?",(uid,)).fetchone(); history=db.execute("SELECT s.class_date,s.sent_at,c.subject FROM sent_reminders s JOIN classes c ON c.id=s.class_id WHERE c.user_id=? ORDER BY s.sent_at DESC LIMIT 20",(uid,)).fetchall(); db.close(); return render_template("settings.html",settings=settings,notification_history=history,user=get_current_user())

@app.post("/settings/save")
@login_required
def save_settings():
    email=request.form.get("email","").strip().lower()
    if not valid_ndmu_email(email): flash("Notification email must be an @ndmu.edu.ph address.","error"); return redirect(url_for("settings"))
    def n(name,default=0):
        try: return int(request.form.get(name,default))
        except ValueError: return default
    ensure_user_settings(current_user_id()); db=get_db(); db.execute("UPDATE notification_settings SET enabled=?,email=?,minutes_before=?,second_minutes_before=?,class_start_enabled=?,morning_digest_enabled=?,commute_minutes=? WHERE user_id=?",(1 if request.form.get("enabled") else 0,email,max(1,min(n("minutes_before",30),1440)),max(0,min(n("second_minutes_before",0),1440)),1 if request.form.get("class_start_enabled") else 0,1 if request.form.get("morning_digest_enabled") else 0,max(0,min(n("commute_minutes",0),180)),current_user_id())); db.commit(); db.close(); flash("Notification settings saved.","success"); return redirect(url_for("settings"))

@app.post("/test-email")
@login_required
def test_email():
    db=get_db(); s=db.execute("SELECT * FROM notification_settings WHERE user_id=?",(current_user_id(),)).fetchone(); db.close()
    if not s or not s["enabled"]: flash("Enable email notifications first.","error"); return redirect(url_for("settings"))
    ok=send_email(s["email"],"📚 Class Schedule App - Test Email","Your Class Schedule email notifications are working.")
    flash("Test email sent successfully!" if ok else "Failed to send test email. Check the server terminal.","success" if ok else "error"); return redirect(url_for("settings"))

@app.post("/change-password")
@login_required
def change_password():
    new=request.form.get("new_password",""); confirm=request.form.get("confirm_password","")
    if len(new)<8 or new!=confirm: flash("Password must be 8+ characters and match confirmation.","error")
    else:
        db=get_db(); db.execute("UPDATE users SET password_hash=? WHERE id=?",(generate_password_hash(new),current_user_id())); db.commit(); db.close(); flash("Password changed.","success")
    return redirect(url_for("settings"))

# Multi-day class entry, with conflict detection and richer class metadata.
@app.post("/add")
@login_required
def add_class():
    days=request.form.getlist("days_of_week"); subject=request.form.get("subject","").strip(); start=request.form.get("start_time",""); end=request.form.get("end_time",""); room=request.form.get("room","").strip() or "NOT SET"
    if not days or not subject or not start or not end or end<=start: flash("Please complete the class details and use a valid time range.","error"); return redirect(url_for("home")+"#add")
    db=get_db(); found_conflict=False
    for raw in days:
        try: day=int(raw)
        except ValueError: continue
        existing=db.execute("SELECT 1 FROM classes WHERE user_id=? AND day_of_week=? AND start_time<? AND end_time>? LIMIT 1",(current_user_id(),day,end,start)).fetchone()
        if existing: found_conflict=True
        db.execute("INSERT INTO classes(user_id,day_of_week,subject,start_time,end_time,room,teacher,color,mode,course_type) VALUES(?,?,?,?,?,?,?,?,?,?)",(current_user_id(),day,subject,start,end,room,request.form.get("teacher","").strip(),request.form.get("color","#8b7cff"),request.form.get("mode","On campus"),request.form.get("course_type","Class")))
    db.commit(); db.close()
    flash("Class added. Review the planner for any overlap warning." if found_conflict else "Class added.","success" if not found_conflict else "error"); return redirect(url_for("home"))

@app.route("/edit/<int:class_id>",methods=["GET","POST"])
@login_required
def edit_class(class_id):
    uid=current_user_id(); db=get_db(); c=db.execute("SELECT * FROM classes WHERE id=? AND user_id=?",(class_id,uid)).fetchone()
    if not c: db.close(); return redirect(url_for("home"))
    if request.method=="POST":
        try: day=int(request.form["day_of_week"])
        except ValueError: day=c["day_of_week"]
        subject=request.form.get("subject","").strip(); start=request.form.get("start_time",""); end=request.form.get("end_time","")
        if day not in range(7) or not subject or not start or not end or end<=start: db.close(); flash("Please enter valid class details.","error"); return redirect(url_for("edit_class",class_id=class_id))
        db.execute("UPDATE classes SET day_of_week=?,subject=?,start_time=?,end_time=?,room=?,teacher=?,notes=?,color=?,mode=?,meeting_url=?,course_type=? WHERE id=? AND user_id=?",(day,subject,start,end,request.form.get("room","").strip() or "NOT SET",request.form.get("teacher","").strip(),request.form.get("notes","").strip(),request.form.get("color","#8b7cff"),request.form.get("mode","On campus"),request.form.get("meeting_url","").strip(),request.form.get("course_type","Class"),class_id,uid)); db.commit(); db.close(); flash("Class updated.","success"); return redirect(url_for("features_planner"))
    db.close(); return render_template("edit.html",class_item=c,user=get_current_user(),days=DAYS)

@app.post("/delete/<int:class_id>")
@login_required
def delete_class(class_id):
    db=get_db(); db.execute("DELETE FROM classes WHERE id=? AND user_id=?",(class_id,current_user_id())); db.commit(); db.close(); flash("Class deleted.","success"); return redirect(url_for("home"))

@app.route("/import")
@login_required
def import_schedule(): return render_template("import.html",days=DAYS,user=get_current_user())

@app.post("/api/classes/import")
@login_required
def import_classes_api():
    payload=request.get_json(silent=True) or {}; items=payload.get("classes",[]); db=get_db(); imported=0; skipped=0; seen=set()
    for item in items:
        try:
            d=int(item.get("day_of_week")); subject=str(item.get("subject","")).strip(); start=str(item.get("start_time","")); end=str(item.get("end_time","")); room=str(item.get("room","")).strip() or "NOT SET"
            if d not in range(7) or not subject or not start or not end or end<=start: skipped+=1; continue
            sig=(d,subject.lower(),start,end,room.lower())
            if sig in seen: skipped+=1; continue
            seen.add(sig); exists=db.execute("SELECT 1 FROM classes WHERE user_id=? AND day_of_week=? AND subject=? AND start_time=? AND end_time=?",(current_user_id(),d,subject,start,end)).fetchone()
            if exists: skipped+=1; continue
            db.execute("INSERT INTO classes(user_id,day_of_week,subject,start_time,end_time,room) VALUES(?,?,?,?,?,?)",(current_user_id(),d,subject,start,end,room)); imported+=1
        except (ValueError,TypeError): skipped+=1
    db.commit(); db.close(); return jsonify(success=True,imported=imported,skipped=skipped)

# Feature pages
@app.route("/planner")
@login_required
def features_planner():
    uid=current_user_id(); view=request.args.get("view","week"); raw=request.args.get("date","")
    try: anchor=datetime.strptime(raw,"%Y-%m-%d").date() if raw else datetime.now(PHILIPPINES).date()
    except ValueError: anchor=datetime.now(PHILIPPINES).date()
    if view=="day": dates=[anchor]
    elif view=="month":
        dates=[]; d=anchor.replace(day=1)
        while d.month==anchor.month: dates.append(d); d+=timedelta(days=1)
    else:
        monday=anchor-timedelta(days=anchor.weekday()); dates=[monday+timedelta(days=i) for i in range(7)]
    db=get_db(); cs=db.execute("SELECT * FROM classes WHERE user_id=? ORDER BY day_of_week,start_time",(uid,)).fetchall(); ev=db.execute("SELECT * FROM personal_events WHERE user_id=? ORDER BY start_at",(uid,)).fetchall(); hs=db.execute("SELECT * FROM holidays WHERE user_id=?",(uid,)).fetchall(); db.close(); data=[]
    for d in dates:
        h=next((x["name"] for x in hs if x["start_date"]<=d.isoformat()<=x["end_date"]),None); data.append((d,h,[] if h else [x for x in cs if x["day_of_week"]==d.weekday()],[e for e in ev if e["start_at"][:10]==d.isoformat()]))
    return render_template("planner.html",user=get_current_user(),data=data,view=view,anchor=anchor,days=DAYS)

@app.route("/tasks",methods=["GET","POST"])
@login_required
def tasks_page():
    uid=current_user_id(); db=get_db()
    if request.method=="POST" and request.form.get("title","").strip():
        db.execute("INSERT INTO tasks(user_id,class_id,title,description,due_at,priority,kind,starred,created_at) VALUES(?,?,?,?,?,?,?,?,?)",(uid,request.form.get("class_id") or None,request.form["title"].strip(),request.form.get("description",""),request.form.get("due_at") or None,int(request.form.get("priority",1)),request.form.get("kind","task"),1 if request.form.get("starred") else 0,datetime.now(PHILIPPINES).isoformat())); db.commit()
    rows=db.execute("SELECT t.*,c.subject FROM tasks t LEFT JOIN classes c ON c.id=t.class_id WHERE t.user_id=? ORDER BY t.done,t.starred DESC,CASE WHEN t.due_at IS NULL THEN 1 ELSE 0 END,t.due_at",(uid,)).fetchall(); classes=db.execute("SELECT * FROM classes WHERE user_id=? ORDER BY subject",(uid,)).fetchall(); db.close(); return render_template("tasks.html",user=get_current_user(),tasks=rows,classes=classes)

@app.post("/tasks/<int:task_id>/toggle")
@login_required
def task_toggle(task_id): db=get_db(); db.execute("UPDATE tasks SET done=1-done WHERE id=? AND user_id=?",(task_id,current_user_id())); db.commit(); db.close(); return redirect(url_for("tasks_page"))

@app.post("/tasks/<int:task_id>/delete")
@login_required
def task_delete(task_id): db=get_db(); db.execute("DELETE FROM tasks WHERE id=? AND user_id=?",(task_id,current_user_id())); db.commit(); db.close(); return redirect(url_for("tasks_page"))

@app.route("/academics",methods=["GET","POST"])
@login_required
def academics():
    uid=current_user_id(); db=get_db()
    if request.method=="POST":
        if request.form.get("action")=="attendance": db.execute("INSERT OR REPLACE INTO attendance(user_id,class_id,class_date,status) VALUES(?,?,?,?)",(uid,request.form["class_id"],request.form["class_date"],request.form["status"]))
        else: db.execute("INSERT INTO grades(user_id,class_id,title,score,max_score,weight,created_at) VALUES(?,?,?,?,?,?,?)",(uid,request.form["class_id"],request.form["title"],float(request.form["score"]),float(request.form["max_score"]),float(request.form.get("weight",0)),datetime.now(PHILIPPINES).isoformat()))
        db.commit()
    classes=db.execute("SELECT * FROM classes WHERE user_id=? ORDER BY subject",(uid,)).fetchall(); grades=db.execute("SELECT g.*,c.subject FROM grades g JOIN classes c ON c.id=g.class_id WHERE g.user_id=? ORDER BY g.created_at DESC",(uid,)).fetchall(); attendance=db.execute("SELECT a.*,c.subject FROM attendance a JOIN classes c ON c.id=a.class_id WHERE a.user_id=? ORDER BY a.class_date DESC",(uid,)).fetchall(); db.close(); return render_template("academics.html",user=get_current_user(),classes=classes,grades=grades,attendance=attendance)

@app.route("/semester",methods=["GET","POST"])
@login_required
def semester():
    uid=current_user_id(); db=get_db()
    if request.method=="POST": db.execute("UPDATE semesters SET is_current=0 WHERE user_id=?",(uid,)); db.execute("INSERT INTO semesters(user_id,name,start_date,end_date,is_current) VALUES(?,?,?,?,1)",(uid,request.form["name"],request.form.get("start_date"),request.form.get("end_date"))); db.commit()
    semesters=db.execute("SELECT * FROM semesters WHERE user_id=? ORDER BY is_current DESC,start_date DESC",(uid,)).fetchall(); db.close(); return render_template("semester.html",user=get_current_user(),semesters=semesters)

@app.route("/holidays",methods=["GET","POST"])
@login_required
def holidays():
    uid=current_user_id(); db=get_db()
    if request.method=="POST": db.execute("INSERT INTO holidays(user_id,name,start_date,end_date) VALUES(?,?,?,?)",(uid,request.form["name"],request.form["start_date"],request.form["end_date"])); db.commit()
    rows=db.execute("SELECT * FROM holidays WHERE user_id=? ORDER BY start_date",(uid,)).fetchall(); db.close(); return render_template("holidays.html",user=get_current_user(),holidays=rows)

@app.route("/events",methods=["GET","POST"])
@login_required
def events():
    uid=current_user_id(); db=get_db()
    if request.method=="POST": db.execute("INSERT INTO personal_events(user_id,title,start_at,end_at,location,notes) VALUES(?,?,?,?,?,?)",(uid,request.form["title"],request.form["start_at"],request.form.get("end_at") or None,request.form.get("location",""),request.form.get("notes",""))); db.commit()
    rows=db.execute("SELECT * FROM personal_events WHERE user_id=? ORDER BY start_at",(uid,)).fetchall(); db.close(); return render_template("events.html",user=get_current_user(),events=rows)

def ics_escape(x): return str(x or "").replace("\\","\\\\").replace(";","\\;").replace(",","\\,").replace("\n","\\n")

@app.route("/calendar.ics")
def calendar_ics():
    token=request.args.get("token"); db=get_db(); u=db.execute("SELECT id FROM users WHERE share_token=?",(token,)).fetchone() if token else None
    if not u and current_user_id(): u=db.execute("SELECT id FROM users WHERE id=?",(current_user_id(),)).fetchone()
    if not u: db.close(); return Response("Unauthorized",401)
    uid=u["id"]; cs=db.execute("SELECT * FROM classes WHERE user_id=?",(uid,)).fetchall(); ev=db.execute("SELECT * FROM personal_events WHERE user_id=?",(uid,)).fetchall(); db.close(); lines=["BEGIN:VCALENDAR","VERSION:2.0","PRODID:-//Class Schedule//EN"]; codes=["MO","TU","WE","TH","FR","SA","SU"]; fmt="%Y%m%dT%H%M%S"
    for c in cs:
        st=datetime(2026,1,5+c["day_of_week"],*map(int,c["start_time"].split(":")),tzinfo=PHILIPPINES); en=datetime(2026,1,5+c["day_of_week"],*map(int,c["end_time"].split(":")),tzinfo=PHILIPPINES)
        lines += ["BEGIN:VEVENT",f"UID:class-{uid}-{c['id']}@classschedule",f"DTSTART;TZID=Asia/Manila:{st.strftime(fmt)}",f"DTEND;TZID=Asia/Manila:{en.strftime(fmt)}",f"RRULE:FREQ=WEEKLY;BYDAY={codes[c['day_of_week']]}",f"SUMMARY:{ics_escape(c['subject'])}",f"LOCATION:{ics_escape(c['room'])}","END:VEVENT"]
    for e in ev:
        st=datetime.fromisoformat(e["start_at"]).replace(tzinfo=PHILIPPINES); en=datetime.fromisoformat(e["end_at"]).replace(tzinfo=PHILIPPINES) if e["end_at"] else st+timedelta(hours=1)
        lines += ["BEGIN:VEVENT",f"UID:event-{uid}-{e['id']}@classschedule",f"DTSTART;TZID=Asia/Manila:{st.strftime(fmt)}",f"DTEND;TZID=Asia/Manila:{en.strftime(fmt)}",f"SUMMARY:{ics_escape(e['title'])}",f"LOCATION:{ics_escape(e['location'])}",f"DESCRIPTION:{ics_escape(e['notes'])}","END:VEVENT"]
    return Response("\r\n".join(lines+["END:VCALENDAR"])+"\r\n",mimetype="text/calendar")

@app.route("/share")
@login_required
def share():
    db=get_db(); r=db.execute("SELECT share_token FROM users WHERE id=?",(current_user_id(),)).fetchone(); token=r["share_token"] or secrets.token_urlsafe(18); db.execute("UPDATE users SET share_token=? WHERE id=?",(token,current_user_id())); db.commit(); db.close(); return render_template("share.html",user=get_current_user(),share_url=url_for("public_share",token=token,_external=True),calendar_url=url_for("calendar_ics",token=token,_external=True))

@app.route("/share/<token>")
def public_share(token):
    db=get_db(); u=db.execute("SELECT id FROM users WHERE share_token=?",(token,)).fetchone()
    if not u: db.close(); return Response("Not found",404)
    cs=db.execute("SELECT * FROM classes WHERE user_id=? ORDER BY day_of_week,start_time",(u["id"],)).fetchall(); db.close(); schedule={d:[dict(x) for x in cs if x["day_of_week"]==i] for i,d in enumerate(DAYS)}; return render_template("public_schedule.html",schedule=schedule,days=DAYS)

if __name__=="__main__":
    init_db()
    scheduler=BackgroundScheduler(timezone=PHILIPPINES)
    scheduler.add_job(check_for_reminders,"interval",minutes=1,id="class_reminder_checker",replace_existing=True)
    scheduler.start()
    print("Notification scheduler started.")
    app.run(host="0.0.0.0",port=5000,debug=True,use_reloader=False)

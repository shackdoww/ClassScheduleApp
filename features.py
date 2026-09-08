from flask import Blueprint, render_template, request, redirect, url_for, session, flash, Response
from functools import wraps
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import sqlite3, secrets
bp=Blueprint('features',__name__); TZ=ZoneInfo('Asia/Manila'); DAYS=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
def db():
 c=sqlite3.connect('schedule.db'); c.row_factory=sqlite3.Row; return c
def uid(): return session.get('user_id')
def auth(fn):
 @wraps(fn)
 def w(*a,**k): return fn(*a,**k) if uid() else redirect(url_for('login'))
 return w
def schema():
 c=db(); cols=[r['name'] for r in c.execute('PRAGMA table_info(classes)').fetchall()]
 for n,s in [('teacher','TEXT DEFAULT ""'),('notes','TEXT DEFAULT ""'),('color','TEXT DEFAULT "#8b7cff"'),('mode','TEXT DEFAULT "On campus"'),('meeting_url','TEXT DEFAULT ""'),('course_type','TEXT DEFAULT "Class"')]:
  if n not in cols: c.execute(f'ALTER TABLE classes ADD COLUMN {n} {s}')
 c.execute('CREATE TABLE IF NOT EXISTS semesters(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,name TEXT,start_date TEXT,end_date TEXT,is_current INTEGER DEFAULT 0)')
 c.execute('CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,class_id INTEGER,title TEXT NOT NULL,description TEXT,due_at TEXT,priority INTEGER DEFAULT 1,kind TEXT DEFAULT "task",done INTEGER DEFAULT 0,starred INTEGER DEFAULT 0,created_at TEXT NOT NULL)')
 c.execute('CREATE TABLE IF NOT EXISTS holidays(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,name TEXT NOT NULL,start_date TEXT NOT NULL,end_date TEXT NOT NULL)')
 c.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,title TEXT NOT NULL,start_at TEXT NOT NULL,end_at TEXT,location TEXT,notes TEXT)')
 c.execute('CREATE TABLE IF NOT EXISTS attendance(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,class_id INTEGER,class_date TEXT,status TEXT,UNIQUE(user_id,class_id,class_date))')
 c.execute('CREATE TABLE IF NOT EXISTS grades(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,class_id INTEGER,title TEXT,score REAL,max_score REAL,weight REAL DEFAULT 0,created_at TEXT)')
 c.commit(); c.close()
@bp.before_app_request
def guard():
 try: schema()
 except sqlite3.Error: pass
def classes(u):
 c=db(); r=c.execute('SELECT * FROM classes WHERE user_id=? ORDER BY day_of_week,start_time',(u,)).fetchall(); c.close(); return r
def holiday(u,d):
 c=db(); r=c.execute('SELECT 1 FROM holidays WHERE user_id=? AND start_date<=? AND end_date>=?',(u,d.isoformat(),d.isoformat())).fetchone(); c.close(); return bool(r)
def overlap(u,day,start,end,exclude=None): return [x for x in classes(u) if x['day_of_week']==day and x['id']!=exclude and start<x['end_time'] and end>x['start_time']]
@bp.route('/hub')
@auth
def hub():
 now=datetime.now(TZ); u=uid(); today=[] if holiday(u,now.date()) else [x for x in classes(u) if x['day_of_week']==now.weekday()]
 future=[]
 for x in classes(u):
  d=now.date()+timedelta(days=(x['day_of_week']-now.weekday())%7); dt=datetime.combine(d,datetime.strptime(x['start_time'],'%H:%M').time()).replace(tzinfo=TZ)
  if dt>now and not holiday(u,d): future.append((dt,x))
 c=db(); tasks=c.execute('SELECT t.*,cl.subject FROM tasks t LEFT JOIN classes cl ON cl.id=t.class_id WHERE t.user_id=? AND t.done=0 ORDER BY t.starred DESC,t.due_at LIMIT 8',(u,)).fetchall(); c.close()
 return render_template('hub.html',user=u,now=now,today=today,next_class=min(future,key=lambda x:x[0]) if future else None,tasks=tasks)
@bp.route('/planner')
@auth
def planner():
 u=uid(); view=request.args.get('view','week'); raw=request.args.get('date','')
 try: anchor=datetime.strptime(raw,'%Y-%m-%d').date() if raw else datetime.now(TZ).date()
 except ValueError: anchor=datetime.now(TZ).date()
 if view=='day': ds=[anchor]
 elif view=='month':
  ds=[]; d=anchor.replace(day=1)
  while d.month==anchor.month: ds.append(d); d+=timedelta(days=1)
 else:
  m=anchor-timedelta(days=anchor.weekday()); ds=[m+timedelta(days=i) for i in range(7)]
 c=db(); cs=c.execute('SELECT * FROM classes WHERE user_id=? ORDER BY day_of_week,start_time',(u,)).fetchall(); es=c.execute('SELECT * FROM events WHERE user_id=? ORDER BY start_at',(u,)).fetchall(); hs=c.execute('SELECT * FROM holidays WHERE user_id=?',(u,)).fetchall(); c.close(); data=[]
 for d in ds:
  h=next((x['name'] for x in hs if x['start_date']<=d.isoformat()<=x['end_date']),None); data.append((d,h,[] if h else [x for x in cs if x['day_of_week']==d.weekday()],[e for e in es if e['start_at'][:10]==d.isoformat()]))
 return render_template('planner.html',user=u,data=data,view=view,anchor=anchor,days=DAYS)
@bp.route('/tasks',methods=['GET','POST'])
@auth
def tasks():
 u=uid(); c=db()
 if request.method=='POST' and request.form.get('title','').strip(): c.execute('INSERT INTO tasks(user_id,class_id,title,description,due_at,priority,kind,starred,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(u,request.form.get('class_id') or None,request.form['title'].strip(),request.form.get('description',''),request.form.get('due_at') or None,int(request.form.get('priority',1)),request.form.get('kind','task'),1 if request.form.get('starred') else 0,datetime.now(TZ).isoformat())); c.commit()
 rows=c.execute('SELECT t.*,cl.subject FROM tasks t LEFT JOIN classes cl ON cl.id=t.class_id WHERE t.user_id=? ORDER BY t.done,t.starred DESC,t.due_at',(u,)).fetchall(); cs=c.execute('SELECT * FROM classes WHERE user_id=? ORDER BY subject',(u,)).fetchall(); c.close(); return render_template('tasks.html',user=u,tasks=rows,classes=cs)
@bp.post('/tasks/<int:i>/toggle')
@auth
def task_toggle(i):
 c=db(); c.execute('UPDATE tasks SET done=1-done WHERE id=? AND user_id=?',(i,uid())); c.commit(); c.close(); return redirect(url_for('features.tasks'))
@bp.post('/tasks/<int:i>/delete')
@auth
def task_delete(i):
 c=db(); c.execute('DELETE FROM tasks WHERE id=? AND user_id=?',(i,uid())); c.commit(); c.close(); return redirect(url_for('features.tasks'))
@bp.route('/class/<int:i>',methods=['GET','POST'])
@auth
def class_edit(i):
 u=uid(); c=db(); row=c.execute('SELECT * FROM classes WHERE id=? AND user_id=?',(i,u)).fetchone()
 if not row: c.close(); return redirect(url_for('home'))
 if request.method=='POST':
  day=int(request.form['day_of_week']); start=request.form['start_time']; end=request.form['end_time']; c.execute('UPDATE classes SET day_of_week=?,subject=?,start_time=?,end_time=?,room=?,teacher=?,notes=?,color=?,mode=?,meeting_url=?,course_type=? WHERE id=? AND user_id=?',(day,request.form['subject'].strip(),start,end,request.form.get('room','').strip() or 'NOT SET',request.form.get('teacher','').strip(),request.form.get('notes','').strip(),request.form.get('color','#8b7cff'),request.form.get('mode','On campus'),request.form.get('meeting_url','').strip(),request.form.get('course_type','Class'),i,u)); c.commit(); c.close(); flash('Class details saved.','success'); return redirect(url_for('features.planner'))
 bad=overlap(u,row['day_of_week'],row['start_time'],row['end_time'],i); c.close(); return render_template('class_editor.html',user=u,class_item=row,days=DAYS,conflicts=bad)
@bp.route('/academics',methods=['GET','POST'])
@auth
def academics():
 u=uid(); c=db()
 if request.method=='POST':
  if request.form.get('action')=='attendance': c.execute('INSERT OR REPLACE INTO attendance(user_id,class_id,class_date,status) VALUES(?,?,?,?,?)',(u,request.form['class_id'],request.form['class_date'],request.form['status']))
  else: c.execute('INSERT INTO grades(user_id,class_id,title,score,max_score,weight,created_at) VALUES(?,?,?,?,?,?,?)',(u,request.form['class_id'],request.form['title'],float(request.form['score']),float(request.form['max_score']),float(request.form.get('weight',0)),datetime.now(TZ).isoformat()))
  c.commit()
 cs=c.execute('SELECT * FROM classes WHERE user_id=? ORDER BY subject',(u,)).fetchall(); gs=c.execute('SELECT g.*,cl.subject FROM grades g JOIN classes cl ON cl.id=g.class_id WHERE g.user_id=? ORDER BY g.created_at DESC',(u,)).fetchall(); at=c.execute('SELECT a.*,cl.subject FROM attendance a JOIN classes cl ON cl.id=a.class_id WHERE a.user_id=? ORDER BY a.class_date DESC',(u,)).fetchall(); c.close(); return render_template('academics.html',user=u,classes=cs,grades=gs,attendance=at)
@bp.route('/semester',methods=['GET','POST'])
@auth
def semester():
 u=uid(); c=db()
 if request.method=='POST': c.execute('UPDATE semesters SET is_current=0 WHERE user_id=?',(u,)); c.execute('INSERT INTO semesters(user_id,name,start_date,end_date,is_current) VALUES(?,?,?,?,1)',(u,request.form['name'],request.form.get('start_date'),request.form.get('end_date'))); c.commit()
 rows=c.execute('SELECT * FROM semesters WHERE user_id=? ORDER BY is_current DESC,start_date DESC',(u,)).fetchall(); c.close(); return render_template('semester.html',user=u,semesters=rows)
@bp.route('/holidays',methods=['GET','POST'])
@auth
def holidays():
 u=uid(); c=db()
 if request.method=='POST': c.execute('INSERT INTO holidays(user_id,name,start_date,end_date) VALUES(?,?,?,?)',(u,request.form['name'],request.form['start_date'],request.form['end_date'])); c.commit()
 rows=c.execute('SELECT * FROM holidays WHERE user_id=? ORDER BY start_date',(u,)).fetchall(); c.close(); return render_template('holidays.html',user=u,holidays=rows)
@bp.route('/events',methods=['GET','POST'])
@auth
def events():
 u=uid(); c=db()
 if request.method=='POST': c.execute('INSERT INTO events(user_id,title,start_at,end_at,location,notes) VALUES(?,?,?,?,?,?)',(u,request.form['title'],request.form['start_at'],request.form.get('end_at') or None,request.form.get('location',''),request.form.get('notes',''))); c.commit()
 rows=c.execute('SELECT * FROM events WHERE user_id=? ORDER BY start_at',(u,)).fetchall(); c.close(); return render_template('events.html',user=u,events=rows)
def esc(x): return str(x or '').replace('\\','\\\\').replace(';','\\;').replace(',','\\,').replace('\n','\\n')
@bp.route('/calendar.ics')
def calendar_ics():
 token=request.args.get('token'); c=db(); u=c.execute('SELECT id FROM users WHERE share_token=?',(token,)).fetchone() if token else None
 if not u and uid(): u=c.execute('SELECT id FROM users WHERE id=?',(uid(),)).fetchone()
 if not u: c.close(); return Response('Unauthorized',401)
 cs=c.execute('SELECT * FROM classes WHERE user_id=?',(u['id'],)).fetchall(); es=c.execute('SELECT * FROM events WHERE user_id=?',(u['id'],)).fetchall(); c.close(); lines=['BEGIN:VCALENDAR','VERSION:2.0','PRODID:-//Class Schedule//EN']; codes=['MO','TU','WE','TH','FR','SA','SU']; fmt='%Y%m%dT%H%M%S'
 for x in cs:
  st=datetime(2026,1,5+x['day_of_week'],*map(int,x['start_time'].split(':')),tzinfo=TZ); en=datetime(2026,1,5+x['day_of_week'],*map(int,x['end_time'].split(':')),tzinfo=TZ); lines += ['BEGIN:VEVENT',f"UID:class-{u['id']}-{x['id']}@classschedule",f'DTSTART;TZID=Asia/Manila:{st.strftime(fmt)}',f'DTEND;TZID=Asia/Manila:{en.strftime(fmt)}',f"RRULE:FREQ=WEEKLY;BYDAY={codes[x['day_of_week']]}",f"SUMMARY:{esc(x['subject'])}",f"LOCATION:{esc(x['room'])}",'END:VEVENT']
 for x in es:
  st=datetime.fromisoformat(x['start_at']).replace(tzinfo=TZ); en=datetime.fromisoformat(x['end_at']).replace(tzinfo=TZ) if x['end_at'] else st+timedelta(hours=1); lines += ['BEGIN:VEVENT',f"UID:event-{u['id']}-{x['id']}@classschedule",f'DTSTART;TZID=Asia/Manila:{st.strftime(fmt)}',f'DTEND;TZID=Asia/Manila:{en.strftime(fmt)}',f"SUMMARY:{esc(x['title'])}",f"LOCATION:{esc(x['location'])}",f"DESCRIPTION:{esc(x['notes'])}",'END:VEVENT']
 return Response('\r\n'.join(lines+['END:VCALENDAR'])+'\r\n',mimetype='text/calendar')
@bp.route('/share')
@auth
def share():
 c=db(); r=c.execute('SELECT share_token FROM users WHERE id=?',(uid(),)).fetchone(); token=r['share_token'] or secrets.token_urlsafe(18); c.execute('UPDATE users SET share_token=? WHERE id=?',(token,uid())); c.commit(); c.close(); return render_template('share.html',share_url=url_for('features.public_share',token=token,_external=True),calendar_url=url_for('features.calendar_ics',token=token,_external=True))
@bp.route('/share/<token>')
def public_share(token):
 c=db(); u=c.execute('SELECT id FROM users WHERE share_token=?',(token,)).fetchone()
 if not u: c.close(); return Response('Not found',404)
 cs=c.execute('SELECT * FROM classes WHERE user_id=? ORDER BY day_of_week,start_time',(u['id'],)).fetchall(); c.close(); return render_template('public_schedule.html',schedule={d:[dict(x) for x in cs if x['day_of_week']==i] for i,d in enumerate(DAYS)},days=DAYS)

def register(app): app.register_blueprint(bp)

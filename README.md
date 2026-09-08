# Class Schedule

A student-focused class schedule and planning app for NDMU institutional users.

## Current features

- Individual accounts restricted to `@ndmu.edu.ph`
- Password hashing and account sessions
- Weekly schedule with multi-day class entry
- Editable class details: teacher, room, notes, color, class type, mode, meeting link
- Schedule conflict warnings
- Paper schedule OCR import
  - phone/tablet: camera capture
  - PC/laptop: image file picker
  - mandatory OCR review/correction before import
  - add/remove/disable rows and raw OCR view
- Student Hub for today's schedule, next class, tasks, and quick tools
- Week, day, and month planner views
- Tasks, homework, exams, priorities, due dates, and completion state
- Personal events for study sessions, meals, errands, and meetings
- Semester management
- Holidays / no-class dates
- Attendance tracking
- Grade tracking
- Email notifications with first/second reminder options and class-start notification
- Optional morning-briefing and commute-time settings stored for notification expansion
- Notification history
- ICS calendar export
- Read-only schedule sharing link
- PWA/offline foundation

## Database migration

After pulling the newer multi-feature version into an existing installation, run:

```bash
python3 migrate_db.py
```

On Windows:

```powershell
python migrate_db.py
```

Then start the Flask application normally.

## Deployment note

For production, run Flask behind Gunicorn and Nginx, keep port 5000 private, use HTTPS, and store `SECRET_KEY` and mail credentials in environment variables.

The current browser version provides the web/PWA foundation. Native Android/iOS push notifications, widgets, lock-screen/live activities, native calendar APIs, and full OAuth calendar synchronization belong in the native-app phase.

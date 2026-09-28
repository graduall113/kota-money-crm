# Staff Attendance (admin dashboard)

Admin-only section in the sidebar (staff-management area, under **Staff**).

| URL | Purpose |
| --- | --- |
| `/staff-attendance/` | Today cards + filterable, paginated table |
| `/staff-attendance/<id>/` | Detail: evidence, anomalies, correction form, audit history |
| `/staff-attendance/<id>/correct/` | POST: admin correction (reason required) |
| `/staff-attendance/export/` | CSV (default) or Excel (`?format=xlsx`) of the current filters |

## Permissions
Every view is `@active_account_required` + `@admin_required`, so a staff member who types the URL gets a 403
(or, if they haven't started their day, the existing attendance gate redirects them). Hiding the sidebar
link is cosmetic only. Staff keep using `/attendance/` for their own day, unchanged.

## Statuses
Working, Full Day, Half Day, Ended (a short day), Auto Ended and the "no record" states Not Started (today) and
Absent (a past date with no record). Not Started / Absent are only shown in the single-date view, and only for
staff who had joined by that date. "Anomaly" means a non-dismissed Start/End/Admin attendance event on that
business date (including a rejected attempt that produced no record).

Absent is simply "active staff, no record that day": there is no holiday / weekly-off calendar in the CRM yet,
so Sundays and holidays will show as Absent.

## Corrections
Start time, end time and status can be corrected; a reason (5+ characters) is mandatory. Times are HH:MM on the
record's own date, not in the future, end >= start; an ended day cannot be re-opened. Duration and status are
recomputed from the times unless a different status is chosen explicitly. Setting an end time on an auto-ended
day clears the Auto Ended flag (logged as its own row). Each changed field is stored in `AttendanceCorrection`
(admin, staff, date, field, old, new, reason, timestamp) and mirrored into the CRM Audit Log, atomically.

## Export
Reuses `crm/exports.py` (streamed CSV with BOM for Excel, or XLSX; formula-injection safe). Columns: Staff, Date,
Start, End, Worked Hours (decimal), Status, Auto Ended, Verification, Anomaly. No IP, device, coordinates,
email or phone. Each export is written to the audit log.

## Performance
Server-side filtering and pagination (25 per page by default); the export streams in chunks. New indexes:
`(work_date, attendance_status)` and `attendance_status` on `Attendance` (staff and work_date were already indexed).
Migration: `0013_staff_attendance_admin`.

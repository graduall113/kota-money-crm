# Holidays (admin only)

Sidebar → **Holidays** (admins only). URL: `/holidays/`.

## What an admin can do
- **Add** a holiday: single day, date range (e.g. 20/10/2026 – 27/10/2026), one week, one month, with a custom name and optional reason.
- **Edit**, **Delete** (with a confirmation page), **Activate / Deactivate** (keeps it on file, but it stops counting).
- Browse **Upcoming / Current / Past / All** tabs; summary cards show today's holiday and the next one.

A holiday is stored as an inclusive `start_date`..`end_date` range, so every date between the two (both included) is a holiday.
Dates are DD/MM/YYYY, like the rest of the CRM. "Today" is the business date (Asia/Kolkata), same as attendance.

## Type presets (the end date is filled in on the server, so it works without JavaScript)
| Type | End date |
|---|---|
| Single day | same as start |
| One week | start + 6 days (7 days total) |
| One month | same date next month − 1 day (starting on the 1st = the whole calendar month) |
| Date range | the date you type |

## Validation
- Start date after end date → error on the End date field (also enforced by a database CHECK constraint).
- Exactly the same dates as another holiday → refused (database unique constraint `uniq_holiday_period`), even if the other one is inactive.
- Same name (case-insensitive) on overlapping dates → refused as an accidental duplicate.
- Overlaps another **active** holiday with a different name → warning listing the clashes; saved only after **Save anyway**.
- A holiday can span at most 366 days; years must be 2000–2100.

## Permissions
Every holiday view is `@active_account_required @admin_required` (`crm/decorators.py`): staff, and anyone not signed in,
are refused server-side (403 / login redirect) on GET *and* POST, whether or not the sidebar link is visible.
`crm.access.can_manage_holidays(user)` is the single definition for templates/tests. Every create / edit / delete /
activate / deactivate is written to the Audit Log (`holiday_created`, `holiday_edited`, `holiday_deleted`,
`holiday_activated`, `holiday_deactivated`).

## For developers
`crm/holidays.py` — `is_holiday(date)`, `holiday_for(date)`, `holidays_on(date)`, `holiday_map(start, end)`.
Only active holidays count.

Attendance integration (enforced server-side):

- **Start Day is refused on an active holiday.** The check lives in `attendance.start_day()`, the single function every
  path uses (staff Start Day, direct/forged POSTs, admin override), so nothing sent from the browser can bypass it.
  The view also checks first, only to give a clean message. Staff see a Holiday card (name + date or range) instead of
  the Start Day button, on the Attendance page and in the sidebar.
- **HOLIDAY, never Absent / Not Started.** A staff member with no record on a holiday date shows as `Holiday` on the
  admin Staff Attendance page (rows, "On Holiday" card, status filter, CSV/Excel export) and in their own history.
- **Holiday declared after someone started.** The record is never deleted or edited. It keeps its normal status
  (Working / Full Day ...) with an extra "Worked — Holiday Declared Later" flag; the person can still End Day.
- **Removing / deactivating the holiday** needs no clean-up: statuses are derived from the holiday table on every
  request, so normal Start Day and Absent rules apply again immediately.
- Staff who cannot start remain behind the existing deny-by-default attendance gate (CRM closed for them that day).
  Auto-end at 19:00 is unchanged.

## Deploying
Additive migration `0014_holiday` (one new table, no existing table touched): `python manage.py migrate`.

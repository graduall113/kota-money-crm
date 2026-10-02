# Staff CRM activity monitoring (mobile)

Measures whether a staff member is **using the CRM on their phone**. It reports only what the CRM page can
verify. It never claims to know which other app is open.

## What the inspection found
| Area | Before this change |
|---|---|
| Attendance | Start Day / End Day state machine, verification, admin dashboard. **No** lunch, heartbeat or activity tracking. Reused unchanged. |
| Calling | `CallRecord` rows are synced by the Android app **after** a call ends. No live call signal exists, so none is faked. |
| Lead/Contact `Activity` | A customer timeline. Unrelated and untouched. |
| Heartbeat / last_seen / visibility / inactivity | Did not exist (only import-job and Android-device heartbeats). Built new. |
| Lunch | Did not exist. Added as a small separate `LunchBreak` model (one per day). |

## How it works
```
Phone browser (static/js/staff_activity.js)           Django (crm/staff_activity.py)
  heartbeat      every 60 s while visible     ─►   StaffPresence.last_heartbeat_at   (alive, not work)
  activity       real touch/typing/scroll/nav ─►   StaffPresence.last_activity_at    (meaningful use)
  visibility     hidden / visible             ─►   StaffPresence.reported_visibility
                                                   StaffInactivityPeriod  (15 min rule)
```
* Identity is always `request.user` from the session. The body only says *kind* and *visible/hidden*. The server clock is the only clock.
* **Activity** is sent only for trusted (`isTrusted`) user input while the page is visible, at most once per 30 s, and never from the warning sheet. Heartbeats, timers and polling never count.
* `active_seconds` credits real elapsed time per interaction (max 60 s each). Hidden/background time adds nothing.

## States (factual wording only)
| State | Meaning |
|---|---|
| CRM Active | page visible, real interaction in the last 2 min |
| CRM Temporarily Inactive | page visible, connected, no interaction for a while (< 15 min) |
| CRM in Background / Not Visible | page last reported hidden (app switch, tab switch or screen lock; the server cannot tell which) |
| Unknown | visible page silent for 2.5–5 min (a missed ping or two is normal on mobile data) |
| CRM Session Disconnected | no heartbeat for 5 min (browser closed, phone asleep or no network; indistinguishable) |
| Lunch Break | valid open lunch (up to 50 min) |
| Not working | no open attendance day |

Screen-locked, browser-closed and network-lost are **not** separately reported, because a web page cannot prove them.
`pagehide` / `beforeunload` are used only as a best-effort hint; the heartbeat timeout is what detects silence.

## 15-minute rule
Staff status becomes **Inactive** when `now − baseline ≥ 15 min`, where baseline is the **latest** of: day start,
last meaningful activity, end of a valid lunch, end of a synced call. Heartbeats are not part of it.
Periods are derived from timestamps, so they are recorded with their **true start** (`last activity + 15 min`) even if the
phone was silent and nobody noticed until later. Stored as `StaffInactivityPeriod` (last activity, start, end, CRM state and
visibility when recorded, covered-by-call seconds). Wording is "CRM inactive for N minutes", never "doing nothing".

**Warning:** from minute 12 the page shows a bottom sheet (numbers come from the server). **I'm Active only closes the
notice** and reports nothing; opening or editing a record resets the timer.

## Calls
A synced `CallRecord` (`started_at → ended_at`) is treated as verified work retroactively: it moves the baseline,
closes an open period it covers (`ended_by = call`) and fills `call_overlap_seconds`. A call that has not synced yet cannot
prevent a warning or period; that is a limit of the current Android sync, not something to fake.

## Lunch
Start/End Lunch buttons in the staff sidebar (one lunch per day). While valid (50 min, `ACTIVITY_LUNCH_MAX_MINUTES`)
no inactivity is counted; idle time before lunch is still recorded and closed at lunch start.

## Deploy
```bash
python manage.py migrate                        # 0017: three new tables, nothing existing changed
python manage.py monitor_staff_activity --loop  # or cron every minute; optional, see below
```
The cron/worker only makes the admin board current for people who went silent. Correctness never depends on it.
Admin board: **Staff → Staff Activity** (`/staff-activity/`).

## Settings (env vars, defaults)
`ACTIVITY_MONITORING_ENABLED=1`, `ACTIVITY_HEARTBEAT_SECONDS=60`, `ACTIVITY_REPORT_MIN_SECONDS=30`,
`ACTIVITY_INACTIVITY_MINUTES=15`, `ACTIVITY_WARNING_MINUTES=12`, `ACTIVITY_ACTIVE_WINDOW_SECONDS=120`,
`ACTIVITY_NETWORK_GRACE_SECONDS=150`, `ACTIVITY_DISCONNECT_SECONDS=300`, `ACTIVITY_LUNCH_MAX_MINUTES=50`.

## Honest limits
* A determined person can script fake taps from the browser console. The page filters untrusted events and the server
  throttles and requires a visible page, but a web page cannot prove a human is present.
* iOS Safari suspends hidden tabs, so "Background" followed by "Disconnected" is expected there.
* Admins are never monitored.


## Live assigned-customer call + Android warning (added)

Separate from the CallLog sync (`calling_api.py` / `CallRecord` are untouched).

* Android reports live call events to `POST /api/staff-activity/live-call/` (`CALL_STARTED|CALL_ACTIVE|CALL_ENDED`,
  stable `session_id`, optional `phone_number`) with the existing device bearer token. The **server** matches the
  number against leads/contacts assigned to *that* device's staff (privileged numbers never qualify). Only the verdict is
  stored (`StaffLiveCallActivity`, no number). A connected, qualifying, fresh call => `ACTIVE`, reason "Assigned customer call",
  overriding browser hidden/background and the inactivity clock. When the call ends the idle clock restarts from its end.
* Stale protection: no refresh for `ACTIVITY_LIVE_CALL_TTL_SECONDS` (180) => the call stops counting. Android refreshes every
  `ACTIVITY_LIVE_CALL_HEARTBEAT_SECONDS` (60).
* `POST /api/staff-activity/status/` is polled by the Android foreground service (`ACTIVITY_DEVICE_POLL_SECONDS`, 60, shortened to
  wake at the 15-minute mark). It returns a warning id when an inactivity period (exactly `ACTIVITY_INACTIVITY_MINUTES`=15) is open
  and not yet acknowledged; the app acks it, so there is one warning per period. Lunch / assigned call / day not started => none.
* The browser's older 12-minute "about to go inactive" banner (`ACTIVITY_WARNING_MINUTES`) is unchanged and unrelated to the Android warning.

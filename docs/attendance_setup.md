# Staff attendance — setup

## What runs where
- **Source of truth:** the `crm_attendance` table (Django). n8n is not involved.
- **Lockout at 19:00 is exact with or without a scheduler**: the middleware
  auto-ends an overdue record the moment that staff member makes any request.
- **The scheduled job** makes sure people who *don't* open the CRM after 19:00
  also get closed (end_time = 19:00, `auto_ended=True`, duration + status
  calculated), so reports are complete.

## Scheduled job (Render)
Create a **Cron Job** service pointing at the same repo/env vars (needs `DATABASE_URL`):

    Build command:  pip install -r requirements.txt
    Command:        python manage.py auto_end_attendance
    Schedule (UTC): */15 * * * *        # 19:00 IST = 13:30 UTC

The command is idempotent and always stamps the official 19:00 as end time, so
running it often (or late) is harmless. Any other scheduler that can run that
command (system cron, another host) works the same way.

## Configuration (env vars, all optional — defaults shown)
| Variable | Default | Meaning |
|---|---|---|
| `ATTENDANCE_TIMEZONE` | `Asia/Kolkata` | business timezone for `work_date` |
| `ATTENDANCE_WORK_START` | `10:00` | shown to staff (start is allowed any time before end) |
| `ATTENDANCE_WORK_END` | `19:00` | auto-end moment; Start Day is refused after it |
| `ATTENDANCE_FULL_DAY_MIN_MINUTES` | `480` | worked ≥ this → Full Day |
| `ATTENDANCE_HALF_DAY_MIN_MINUTES` | `240` | worked ≥ this → Half Day, below → Short Day |
| `ATTENDANCE_ENFORCED` | `1` | `0` = emergency off-switch (nobody locked) |

## Deploy
`build.sh` already runs `migrate` (adds migration `0011_attendance`). No new packages.

## Locked / allowed URLs
Non-admin staff without an ACTIVE day can reach only: login, register, logout,
password reset, `/attendance/` (+ start/end), `/profile/`, `/settings/` (account
section). `/api/*` (n8n + Android app) and `/admin/` are never gated. Every other
URL — including any added later — is blocked by `crm.middleware.AttendanceRequiredMiddleware`.

---

# Attendance verification (Feature 5)

Defence in depth on **Start Day**. The backend decides everything; the browser only *measures*
(latitude / longitude / accuracy). It raises the bar against ordinary misuse (starting from home,
edited requests, borrowed accounts, replays). It cannot make browser attendance impossible to cheat:
GPS can be spoofed on a rooted phone, a VPN can fake an IP, a cookie can be copied. That is why
everything odd is also written to a neutral, reviewable trail.

## Turn it on (nothing is enforced until you do)
1. `python manage.py migrate` (adds `0012_attendance_verification`).
2. Settings → **Attendance Verification**: office latitude/longitude, radius, accuracy threshold,
   optional office IPs, then tick the checks to require. Or store the coordinates from the shell:
   `python manage.py set_office_location --lat <latitude> --lng <longitude> --radius 200`
   (coordinates live in the database, never in code; this does not enable enforcement).
3. Trusted devices (optional): **Attendance Devices** → pick staff → *Generate enrolment code*.
   The person logs in on the device to trust and enters the code on their Attendance page.

## Layers
| Layer | Setting | Behaviour |
|---|---|---|
| Geofence | Require geofence | Server computes distance to the office; outside radius → rejected + event |
| GPS accuracy | Accuracy threshold | Worse than threshold → "try again" (never a permanent block) |
| Office IP | Require office IP + allow-list | IPs / CIDRs (v4/v6). Leave empty if not static |
| Trusted device | Require trusted device | Admin-issued single-use code → random secret in an HttpOnly cookie (hash stored). Revocable |

- **End Day is never blocked.** Location/IP/device are recorded; anything odd is flagged for review.
- Server time only (Asia/Kolkata): `start_time`, `end_time`, `work_date`, duration, status.
- Staff identity is always `request.user`; fields like `staff_id`, `work_date`, `timestamp`,
  `is_inside_office` are ignored and noted as "Unexpected Input Ignored".
- Failed attempts: neutral events (Location Outside Office, Poor GPS Accuracy, IP Not Allowed,
  Device Mismatch, Repeated Attempt, Duplicate Start/End, ...). After 10 rejected attempts in 10 minutes
  there is a short cool-down; it expires by itself.
- **Admin override** (Attendance Review): starts a day at the server's current time with a mandatory
  reason; marked "Admin override" on the record and audit-logged.
- Attendance/location data is not sent to n8n or Google Sheets.
- Trusted devices are separate from **Calling Devices** (which use the Android app's bearer token,
  something a browser cannot present); Calling Devices are untouched.

## Configuration to check
| Item | Where | Note |
|---|---|---|
| `ATTENDANCE_TRUSTED_PROXY_COUNT` | env (default `1`) | Number of proxies in front of Django (Render = 1; +1 with Cloudflare). Check that the IP shown on the settings page is your real public IP. `0` = ignore `X-Forwarded-For` |
| HTTPS | hosting | Browsers only give location on HTTPS |
| Accuracy threshold | Settings | Desktop Wi-Fi positioning can be 50–500 m; tune, or rely on office IP for desktops |

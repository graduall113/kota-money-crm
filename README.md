# Kota Money CRM

## Run it

    pip install -r requirements.txt
    python manage.py migrate
    python manage.py createsuperuser     # this account is auto-promoted to Admin
    python manage.py runserver

Open http://127.0.0.1:8000/ — you'll be redirected to /login/.

Anyone who registers through /register/ becomes STAFF by default. To make
someone an Admin, sign in as an Admin and use Staff → Edit → Role.

## Roles

- **Admin** — All Leads, Staff Management, Reports, plus everything Staff can do.
- **Staff** — Dashboard, My Leads, Follow-ups, Settings. Cannot reach
  admin-only URLs even by typing them directly (enforced in `crm/decorators.py`,
  not just hidden in the sidebar).

## "My Leads" — read this before touching the Lead model

`reference_by` = who referred the lead. `assigned_to` = who's handling it.
**My Leads always filters on `reference_by`, never `assigned_to`.** A lead
assigned to you that someone else referred does NOT show up in your My Leads.
This is tested in `crm/models.py`'s field comments and was verified against
the exact Rahul/Mohit/Suresh scenario from the spec before shipping.

## Where things are

- `crm/models.py` — `StaffProfile` (role/status) and `Lead`
  (`reference_by` / `assigned_to` as separate FKs, `SET_NULL` so deleting a
  staff account never deletes lead history). Also carries the outbound
  n8n sync-tracking fields: `n8n_sync_status`, `n8n_last_sync`, `n8n_error`.
- `crm/signals.py` — auto-creates a `StaffProfile` for every new `User`
  (superusers become Admin automatically).
- `crm/decorators.py` — `@admin_required` / `@active_account_required`,
  enforced server-side on every admin/staff view.
- `crm/forms.py` — register, login, profile, admin staff create/edit forms,
  and `LeadForm` (the native Add New Lead form).
- `crm/views.py` — all auth + CRM views, including the My Leads filter and
  `lead_create` (the native Add New Lead view).
- `crm/n8n_integration.py` — **outbound** Django → n8n webhook call. Called
  by `lead_create` right after a Lead is saved; POSTs to
  `settings.N8N_LEAD_WEBHOOK_URL` and records the result on the Lead.
- `crm/api.py` — **inbound** n8n → Django endpoint (`/api/leads/create/`,
  authenticated via `X-N8N-API-KEY`). Kept as-is; not used by the native
  form, but left intact in case anything else still posts to it.
- `templates/auth/` — login, register, profile, password reset flow.
- `templates/staff/` — admin Staff Management (list, add, edit, toggle status).
- `templates/leads/lead_form.html` — the native "Add New Lead" page.
- `kota_money/settings.py` — `N8N_LEAD_WEBHOOK_URL` (env var
  `N8N_LEAD_WEBHOOK_URL`, defaults to the Kota Money production webhook) is
  the single source of truth for where new leads get sent. `N8N_FORM_URL`
  is kept for reference but is no longer used by the "Add New Lead" link.

## Edit / Delete Lead

- **Edit Lead** (`/leads/<id>/edit/`) reuses `LeadForm` — the exact same
  fields, dropdowns, and required-field rules as Add New Lead. It does
  **not** re-fire the n8n webhook (that only happens once, at creation).
- **Delete Lead** (`/leads/<id>/delete/`) is a GET-to-confirm /
  POST-to-delete flow (`templates/leads/lead_confirm_delete.html`), never
  a one-click delete, and is CSRF-protected like every other form here.
- Permission rule (`crm/views.py:_can_manage_lead`): **Admins** can edit/
  delete any lead. **Staff** can only edit/delete leads where
  `reference_by == request.user` — same ownership boundary "My Leads"
  already uses. Enforced server-side (`PermissionDenied` → 403), not just
  hidden buttons — hitting the URL directly as a non-owner is blocked too.
- Both actions are exposed as an **Actions** column (Edit / Delete links)
  on both All Leads and My Leads.

## Add New Lead → n8n automation

"Add New Lead" in the sidebar now opens the CRM's own native form
(`/leads/add/`), not the external n8n form. On submit:

1. Django validates the form and saves the Lead to the database (source
   of truth — this always happens first).
2. Django POSTs the saved lead as JSON to `N8N_LEAD_WEBHOOK_URL`
   (`crm/n8n_integration.py:send_lead_to_n8n`).
3. If n8n accepts it, the lead's `n8n_sync_status` is set to `success`
   and the user sees "Lead created successfully and automation started."
4. If n8n is slow, down, or errors, the lead **stays saved** —
   `n8n_sync_status` is set to `failed` with the error recorded in
   `n8n_error`, and the user sees "Lead saved successfully, but
   automation sync is pending." (never a raw error page).

Set the webhook URL via environment variable before deploying:

    export N8N_LEAD_WEBHOOK_URL="https://soyacil.app.n8n.cloud/webhook/kota-money-lead"

If unset, it falls back to that same production URL by default.

## Lead ID

Every Lead has a permanent, human-readable ID such as `KM-1058`
(`Lead.display_id`). It is derived from the database primary key, so it is
assigned by the database on INSERT, is unique even under concurrent creation,
never changes and is never reused. It is read-only: not a form field, not
accepted by the inbound API, and not editable in the admin. Add New Lead shows
"Generated automatically when you save" (no fake ID); the ID appears in the
success message and on the lead page. Search accepts `KM-1058` or `1058`.

n8n receives it as `lead_reference_id` (the numeric `lead_id` is unchanged) plus
an `Idempotency-Key: lead-KM-1058` header. To have it land in a Google Sheet
`Lead ID` column without duplicate rows on retry, follow
`docs/n8n_lead_id_setup.md`.

## Password reset

Uses Django's built-in password-reset flow. In development, reset emails
print to the console (`EMAIL_BACKEND` in `settings.py`) — swap that for
real SMTP before deploying.

## Security notes before deploying

- Set `DEBUG = False` and a real `SECRET_KEY` in `kota_money/settings.py`.
- Set `ALLOWED_HOSTS` to your actual domain.
- Point `EMAIL_BACKEND` at a real mail provider for password resets.


## Attendance verification
Office geofence, GPS accuracy, office IP and trusted-device checks for Start Day, with an admin review trail and override. Off until configured under Settings → Attendance Verification. See `docs/attendance_setup.md`.

## Staff Attendance admin dashboard

See [docs/staff_attendance.md](docs/staff_attendance.md). Apply the new migration with `python manage.py migrate`.

## Google Sheet row colours (Approved / Rejected / Processing)

Row colouring by Status is done by an Apps Script in `google_sheets_row_colour/` that n8n calls after
the Sheets upsert; the row is found by Lead ID. No Django changes. Setup: `docs/n8n_sheet_row_colour_setup.md`.

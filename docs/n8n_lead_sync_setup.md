# Lead → n8n → Google Sheets sync (idempotent upsert by Lead ID)

Django is the source of truth. The Google Sheet is a synchronised copy keyed by the
permanent **Lead ID** (`KM-1050`), never by phone number.

## What Django sends (POST to the n8n webhook, on every sync)

All old keys are unchanged. New keys are additive.

```json
{
  "event": "lead_upsert",
  "lead_id": 1050,
  "lead_reference_id": "KM-1050",
  "idempotency_key": "lead-KM-1050",
  "sync_reason": "lead_edited,status_changed",
  "is_new_lead": false,
  "date": "27/09/2026", "name": "...", "contact_no": "...", "work_profile": "...",
  "income": "...", "requirement": "...", "loan_amount": "2500000", "bank_calling": "...",
  "status": "Processing", "reference_by": "...", "assigned_to": "...",
  "assigned_to_staff": "Rahul", "reference_by_staff": "Amit",
  "email": "", "city": "", "source": "", "interest": "", "next_followup_date": "",
  "contact_id": 500, "updated_at": "30/09/2026 10:15"
}
```
Header `Idempotency-Key: lead-KM-1050` is identical on every send for that lead.
`sync_reason` values: `created`, `converted`, `lead_edited`, `status_changed`,
`assignment_changed`, `reference_changed`, `followup_changed`, `contact_edited`,
`lead_saved`, `manual_resync`.

## n8n workflow (manual — it lives in your n8n Cloud, not in this repo)

1. **Webhook node** → *Respond*: **When Last Node Finishes** (NOT "Immediately").
   Otherwise a Google Sheets failure never reaches Django and the sync is wrongly marked
   successful, so it is never retried.
2. **Google Sheets node** → Operation **Append or Update Row**, *Column to match on* =
   `Lead ID`, value `{{ $json.body.lead_reference_id }}`. Format the Lead ID column as Plain text.
3. Map the columns: Lead ID, Date, Name, Contact No., Work Profile, Income, Requirement,
   Loan Amount, Bank Calling, Status, Reference By, Assigned To (+ any extra you want:
   Email, City, Source, Interest, Next Follow-up, Assigned To (Staff)).
4. **Gmail / WhatsApp nodes**: put an **IF** node before them: `{{ $json.body.is_new_lead }}` is true.
   Otherwise every edit re-sends the new-lead email.
   *Alternative:* build a second small workflow (webhook → Sheets upsert only) and set
   `N8N_LEAD_UPDATE_WEBHOOK_URL` in Django; edits then go there and only new leads / conversions
   use the original workflow.
5. Save and **Activate** the workflow.

## Existing Sheet rows
Rows without a Lead ID are not matched: the first edit of such a lead appends a new row.
One-time fix: fill the `Lead ID` column for old rows (`KM-` + database id; the CRM Lead export
has a `Lead ID` column). The same applies to leads created through the n8n form (`api.py`):
make that workflow write `lead_reference_id` (returned by `/api/leads/create/`) into the Sheet.

## Operations

```bash
python manage.py migrate                      # adds LeadSyncEvent + Lead.n8n_payload_hash (additive)
python manage.py process_lead_sync            # deliver everything due, then exit
python manage.py process_lead_sync --status   # counts + recent errors
python manage.py process_lead_sync --requeue-failed
python manage.py process_lead_sync --enqueue KM-1050,KM-1051   # force re-send
python manage.py process_lead_sync --loop     # worker mode
```
Schedule `process_lead_sync` every minute (cron / Render cron job) so retries are guaranteed even
if the web process restarted. Retries: after 1, 5, 15, 60, 180 min; 6 attempts total, then the event
is `failed` (visible in Django admin → Lead sync events; a later edit or `--requeue-failed` starts again).

Settings / env: `N8N_LEAD_WEBHOOK_URL` (existing), `N8N_LEAD_UPDATE_WEBHOOK_URL` (optional),
`CRM_LEAD_SYNC_MIN_INTERVAL` (default 1 s between sends, protects the Sheets write quota),
`CRM_LEAD_SYNC_AUTODISPATCH` (off automatically under `manage.py test`).

## How duplicates are prevented
* CRM side: one pending + one processing event per Lead (DB constraints); unchanged saves are skipped
  (content hash); converted-contact edits update the existing Lead and never create a Lead or a new ID.
* Sheet side: Append-or-Update-Row on Lead ID. This is the actual guard; Django cannot enforce it.

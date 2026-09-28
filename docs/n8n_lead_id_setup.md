# n8n + Google Sheets: Lead ID setup (manual steps)

The n8n workflow lives in your n8n Cloud account, not in this repository, so it
has to be updated by hand. Django now sends this (all old keys unchanged):

```json
{
  "lead_id": 1058,                 // numeric DB id — unchanged, still a number
  "lead_reference_id": "KM-1058",  // NEW: the human-readable Lead ID
  "date": "27/09/2026", "name": "...", "contact_no": "...", "work_profile": "...",
  "income": "...", "requirement": "...", "loan_amount": "...", "bank_calling": "...",
  "status": "New", "reference_by": "...", "assigned_to": "..."
}
```

plus an HTTP header `Idempotency-Key: lead-KM-1058` (identical on every delivery
attempt for the same lead). This is sent for **both** Add New Lead and
Contact → Convert to Lead, because both go through the same view.

## 1. Add the column to the Google Sheet
Add a header called exactly `Lead ID` (recommended: first column, so it reads
`Lead ID | Date | Name | Contact No. | …`). n8n maps by header name, so inserting
or moving the column does not disturb the existing ones. Format the column as
**Plain text**.

## 2. Update the Google Sheets node
1. Open the workflow → the Google Sheets node that writes the lead row.
2. Change **Operation** to **Append or Update Row**
   (if it is currently *Append Row*, this is the change that stops duplicates).
3. Set **Column to match on** = `Lead ID`.
4. Click **Refresh columns**, then map the new column.
   Use the same expression style your existing columns use. If `Name` is
   `{{ $json.body.name }}`, then:

   | Sheet column | Value |
   |---|---|
   | Lead ID | `{{ $json.body.lead_reference_id }}` |

5. Leave every other column mapping exactly as it is.

With *Append or Update Row* matching on `Lead ID`, a repeated delivery of the
same lead (n8n retry, manual re-run, network replay) **updates its existing row**
instead of adding a second one.

## 3. Also check
* Gmail / WhatsApp nodes: nothing needs to change. To show the ID in an email,
  use `{{ $json.body.lead_reference_id }}`.
* If a node still uses `lead_id`, it keeps receiving the same number as before.
* Save and **activate** the workflow (the CRM calls the Production URL).

## 4. Existing rows (one-time, optional)
Rows written before this change have no Lead ID. They are not touched and not
duplicated. If you want them filled in, the Lead ID for a lead is `KM-` + its
database id; the CRM's Lead export already contains a `Lead ID` column you can
copy across by phone number.

If your sheet *already* had a column holding the numeric `lead_id` (e.g. `1058`),
either keep it under its own header, or rewrite those cells to `KM-1058` once,
so "Column to match on = Lead ID" recognises the old rows and does not append
them again.

## 5. Test
1. Add a lead in the CRM → one new row, `Lead ID` = the ID shown on the lead page.
2. In n8n, open that execution and use **Retry** / re-run → still one row.
3. Convert a Contact to a Lead → one new row with its own, different Lead ID.

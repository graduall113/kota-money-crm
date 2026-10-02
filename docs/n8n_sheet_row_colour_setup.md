# Google Sheet row colour by Status (Lead ID → row → colour)

**No Django / CRM change is needed.** Django already POSTs `lead_reference_id` (`KM-1001`) and
`status` (`Approved`, `Rejected`, `Processing`, `New`, ...) on every create / edit / status change.
The existing webhook, payload and retry queue are untouched.

```
CRM ─▶ n8n Webhook ─▶ Google Sheets "Append or Update Row" (match: Lead ID)   ← unchanged
                              └──────▶ HTTP Request ─▶ Apps Script web app
                                                        find row by Lead ID → colour whole row by Status
```

| Status | Row colour |
|---|---|
| Approved | light green `#D9EAD3` |
| Rejected | light red `#F4CCCC` |
| Processing | light yellow `#FFF2CC` |
| anything else (New, Documents Pending, blank ...) | default / no fill |

The header row is never touched; the row is found by the **Lead ID column header** (not a row number or
column letter), so inserting/sorting rows or columns is safe. Text colour is not changed.

## Step 1 – Install the Apps Script (5 min)
1. Open the Google Sheet → **Extensions → Apps Script**.
2. Replace the contents of `Code.gs` with `google_sheets_row_colour/Code.gs`. Save.
3. Top of the file, `CONFIG`: set `SHEET_NAME` to the exact tab name n8n writes to (leave `''` if it is the first tab).
   Check `LEAD_ID_HEADER` = `Lead ID` and `STATUS_HEADER` = `Status` match your header text.
4. **Project Settings (gear) → Script properties → Add**: `WEBHOOK_SECRET` = a long random string.
   (Only if the script is *not* opened from the Sheet: also add `SPREADSHEET_ID`.)
5. **Deploy → New deployment → type Web app** → *Execute as*: **Me** → *Who has access*: **Anyone** → Deploy.
   Authorise when asked. Copy the **Web app URL** (ends in `/exec`).
6. Sanity check: open that URL in a browser → you should see `{"ok":true,"service":"Kota Money row colouring",...}`.

> After you later edit the script, use **Deploy → Manage deployments → ✏️ → Version: New version → Deploy**.
> The `/exec` URL stays the same. "Anyone" access is safe here because every request must carry `WEBHOOK_SECRET`.

## Step 2 – Add the colour step in n8n
Do this in **every** workflow that writes the Sheet (the main one, and the update-only one if you use
`N8N_LEAD_UPDATE_WEBHOOK_URL`). Do not touch the Webhook node or the Google Sheets node.

1. Open the workflow, select the **Google Sheets** node (Append or Update Row).
2. Copy `google_sheets_row_colour/n8n_colour_nodes.json`, paste it onto the canvas (Ctrl+V).
   *(If n8n rejects the paste, add the nodes by hand: an **HTTP Request** node, an **IF** node and a **Stop and Error** node, configured as below.)*
3. Connect **Google Sheets → Colour Sheet Row** (add it as an *extra* output; leave the existing connection to your Gmail/IF nodes as it is).
4. Open **Colour Sheet Row** and set:
   * **Method** POST, **URL** = your Apps Script `/exec` URL.
   * **Send Body** on, **Body Content Type** JSON, **Specify Body** = *Using JSON*, value (Expression):
     ```
     {{ { secret: 'YOUR_WEBHOOK_SECRET', lead_reference_id: $('Webhook').first().json.body.lead_reference_id, status: $('Webhook').first().json.body.status } }}
     ```
     Replace `Webhook` with the **exact name** of your trigger node. It must read from the trigger node, because after the Sheets node `$json` is the sheet row, not the webhook body.
   * **Options → Redirects → Follow Redirects** on (Apps Script answers through a redirect; n8n does this by default).
5. **Colour OK?** checks `{{ $json.ok }}` is true; the *false* output goes to **Stop: colouring failed**.
6. Workflow **Settings → Respond**: keep **When Last Node Finishes** (as already required by `n8n_lead_sync_setup.md`).
7. **Save and keep the workflow Active.** Use the Production webhook URL as before.

### Choose how a colouring failure is handled
* **Update-only workflow (no email/WhatsApp) — strict:** keep the IF + Stop and Error nodes. If colouring fails,
  n8n returns an error, Django's retry queue re-sends (1, 5, 15, 60, 180 min) and the row gets fixed. Safe, because
  the Sheets upsert is idempotent.
* **Main workflow that also sends Gmail/WhatsApp — lenient:** on **Colour Sheet Row** open **Settings → On Error →
  Continue** and delete the IF/Stop nodes. Otherwise a colouring failure after the email was sent would make Django
  retry and could send the new-lead email twice. The next edit of that lead recolours it, and so does the optional
  safety net below.

### Optional safety net
Apps Script editor → **Triggers (clock icon) → Add Trigger** → function `recolourAllRows` → *Time-driven* → every
10 minutes (or hourly). Any row whose colour drifted (manual edits, a missed call) is corrected from its Status.
The sheet also gets a **Kota Money → Recolour all rows by Status** menu for a one-off pass over existing rows.

## What happens in each scenario
| Scenario | Result |
|---|---|
| New lead | Sheets node appends the row → colour step finds it by Lead ID → coloured by Status (New = no fill). |
| Lead edit | Same row updated → recoloured from current Status. |
| Approved → Processing | same row green → yellow. Processing → Rejected: yellow → red. |
| Repeated / retried sync | Sheets node updates the same row; colouring the same row again changes nothing. No duplicates. |
| Unknown / blank status | Row fill reset to default. |
| Lead ID not in Sheet | Script answers `NOT_FOUND` (nothing coloured, nothing created) → handled per the strict/lenient choice above. |
| Rows inserted or sorted in the Sheet | Row is still found by Lead ID; nothing uses a row number. |

## Test (2 minutes)
1. In the CRM create a lead → new row, no colour (status New).
2. Change its Status to **Approved** → same row turns light green. Then **Processing** → yellow. Then **Rejected** → red.
3. Edit the name several times → still one row, colour matches Status.
4. Header row stays uncoloured. In n8n, **Retry** an execution → still one row.
5. In n8n **Executions**, open *Colour Sheet Row*: output should show `{"ok": true, "rows_coloured": 1, ...}`.

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| `Unauthorized.` | `secret` in n8n ≠ `WEBHOOK_SECRET` script property. |
| `WEBHOOK_SECRET is not set` | Add it under Project Settings → Script properties. |
| `Header "Lead ID" not found` | Header text differs from `CONFIG.LEAD_ID_HEADER`, or `SHEET_NAME` points at the wrong tab. |
| `Lead ID ... is not in the Sheet` | Sheet node wrote to a different tab / ID column empty (old rows without Lead ID: fill them, see `n8n_lead_id_setup.md`). |
| Changes to the script have no effect | Deploy a **new version** of the existing deployment. |
| HTTP node returns HTML / 401 | Deployment access must be **Anyone**, executing as **Me**. |
| `Duplicate Lead ID` (`duplicate_lead_id: true` in the response) | Two rows share the ID (an old manual entry). Both are coloured; delete the extra row. |

Editing colours: change hex codes in `CONFIG.COLOURS`, redeploy, run **Recolour all rows by Status**.

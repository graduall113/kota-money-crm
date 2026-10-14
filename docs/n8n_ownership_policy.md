# n8n leads: nurturing assignee vs. CRM ownership

Four different things are stored on a Lead and must never be confused:

| What | Field | Type | Who/what sets it |
|---|---|---|---|
| Nurturing assignee ("Assigned To") | `Lead.assigned_to_name` | free text, max 200 | typed on the Add/Edit form, or `assigned_to` in the n8n payload |
| Reference by (text) | `Lead.reference_by_name` | free text, max 200 | typed on the form, or `reference_by` in the n8n payload |
| **CRM owner** (who sees / edits it) | `Lead.assigned_to` | FK to a CRM user | creator (staff) / admin's pick on the form; admin assign / transfer; `crm_owner` in the n8n payload |
| CRM referrer / creator | `Lead.reference_by`, `Lead.created_by` | FK to a CRM user | the logged-in creator of a CRM-form lead; **empty** for n8n leads |

## Rules

1. `assigned_to_name` / `reference_by_name` are **free text**. They may be a CRM staff member, an external partner or
   anybody. They are never required to match CRM Staff, never rejected for not matching (only blank-on-the-form and
   >200 characters are validation errors), and no user account is ever created from them.
2. They are **never used to decide CRM ownership**. A name that equals a username, e-mail or full name of a CRM user
   grants that user nothing: no visibility, no edit, no delete, no "referrer keeps access".
3. An inbound n8n lead has no logged-in creator, so it is created **unowned** (`assigned_to` empty). Admins see it
   (All Leads, filter "Unassigned") and assign it with the normal assign / edit / bulk-assign tools, which write
   Assignment History and the audit log. Non-admin staff cannot see or open it until it is assigned to them.
4. The only way to set the owner at creation is the explicit, separate payload key **`crm_owner`**: a username or
   e-mail of exactly one **active** staff member (exact / case-insensitive; no full-name or fuzzy matching). If it is
   missing, blank, unknown, ambiguous or inactive, the lead is **still created, unowned**, and the 201 response
   carries `crm_owner_assigned: false` and a `warnings` list. A valid `crm_owner` writes Assignment History
   ("n8n") and `original_assigned_to`.
5. Editing `assigned_to_name`, assigning or transferring a lead never changes the other: assign / transfer change only
   the owner FK (+ history); editing the text never moves ownership.
6. The Google Sheet "Assigned To" column and the outbound payload `assigned_to` are the free text; the CRM owner is
   sent as `assigned_to_staff` and exported as "Assigned To (CRM)".

## n8n payload

```json
{ "customer_name": "...", "contact_number": "...", "assigned_to": "Any name (free text)",
  "reference_by": "Any name (free text)", "crm_owner": "optional-username-or-email" }
```
`crm_owner` is optional. Do not add it to the n8n workflow unless the form can supply a real CRM username or e-mail.

## Behaviour change vs. the previous revision

The previous revision set the owner FK (and the referrer FK) whenever the free-text name happened to equal a CRM
username / e-mail / full name. That was removed: it let a typed name hand a lead to the wrong person (an admin's
username, an inactive account, or whichever of two same-named users came first) and let a "Reference by" name grant
delete rights. Leads already in the database are **not** modified; no migration, no data change.
Tests: `crm/tests_n8n_ownership.py`.

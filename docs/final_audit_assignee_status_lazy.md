# Final implementation audit — Contact statuses, Assigned To search, KPI audit, lazy loading

How to run the tests (production settings force HTTPS, which makes the plain-HTTP test client get 301s):

    python manage.py test --settings=kota_money.settings_test

## 1. Contact statuses
`Switched Off` (`switched_off`) and `Not Received` (`not_received`) added to `Contact.STATUS_CHOICES` (migration
`0020`, choices only — no data change). Every contact status surface reads that one list, so the Add/Edit form,
detail status dropdown, list/segment filters, bulk "Change status", status badges, CSV/Excel export and activity log
pick them up. Badges: both render as "warn". Lead statuses are untouched. `Contacted` in Performance analytics still
means "status is not New" (an unreached call attempt counts as contacted) — unchanged, flagged as a business choice.

## 2. Contacts label
"Assigned To" -> **Assigned To Staff** on the Contacts list (filter + column), contact detail ("Current Assigned To
Staff"), Segment detail (contacts table) and the Contacts CSV/Excel export header. Stored field (`current_assigned_to`)
and behaviour unchanged. Leads keep "Assigned To".

## 3. Which field is the lead's future-nurturing assignee
`Lead.assigned_to_name` (free text typed on the Add Lead form, required; the "Assigned To" column of the Google
Sheet). NOT `Lead.assigned_to` (CRM owner FK; defaults to the creating staff member, drives permissions; the Sheet
calls it "Assigned To (CRM)"), NOT `reference_by(_name)`, NOT `created_by`. Evidence: `lead_create` sets the FK to the
creator, the Sheet sync keeps the two columns separate, `lead_sync` tests pin it.
Because the old FK filter was labelled "Assigned To" in the admin filter bar, it is now labelled **CRM Owner (staff)**
(same query parameter `assigned_to`, same behaviour). The lead table gets an **Assigned To** column (the name) next to
**CRM Owner** (the FK). The lead export gains `Assigned To` (name) and keeps the FK as `Assigned To (CRM)` — note this
changes the meaning of the existing export column `Assigned To`; it now matches the Sheet.

## 4. Assigned To search behaviour (`crm/assignee_search.py`, `static/js/assignee_filter.js`)
* Parameter `assignee`, on All Leads and My Leads, for admins and staff. Suggestions endpoint `/leads/assignee-suggest/`.
* Matching: casefolded, whitespace collapsed. All stored spellings that normalise to the same name are grouped
  ("Anchal Jain", "anchal jain ", "ANCHAL   JAIN" -> one suggestion, counts summed). The filter then runs an exact
  `assigned_to_name IN (those spellings)` on an indexed column (migration `0021`). Stored names are never modified.
* Typed text that matches no complete name falls back to "contains" ("anchal" -> every Anchal). Text matching nothing
  returns zero rows (never everything).
* Ranking: exact, prefix, word-prefix, substring, then close-spelling (difflib ratio >= 0.74, only for 3+ characters,
  labelled "Did you mean?"). Max 8 suggestions. Empty input shows the most-used assignees.
* Bounded: one GROUP BY over the caller's authorised leads (capped at 2,000 distinct names), ranking in memory over that
  short list. Browser: 250 ms debounce, previous request aborted, results memoised per page.
* Authorisation: suggestions and the filter run on `access.visible_leads(user)`; a staff user never sees a name that
  exists only on leads they can't open. Endpoint is login-required and `Cache-Control: private, no-store`.
* Works with other filters, scope tabs, sort, pagination, export (`jobs.resolve_queryset` / export use `filter_leads`),
  "select all N matching" and the My Leads KPI cards (same `filter_leads`).

## 5. KPI inventory
The previous inventory in `SUMMARY_SYNC_CHECKLIST.md` was re-audited against the code; every card there is still
classified correctly and its tests pass. Additions in this change: My Leads cards follow the new `assignee` filter
(test `test_my_leads_scope_and_kpis_follow_assignee`, `test_pagination_independent_totals`). All Leads and Contacts
show only a record count (filtered, pagination-independent). Intentionally global (unchanged): Dashboard, Reports,
Follow-ups, Staff detail, Import/Batch counters, Attendance-events open badge, Holidays. Filter-dependent: My Leads,
Segment detail, Staff Attendance, Calling analytics, Document analytics (all unchanged and tested).

## 6. Lazy-loading audit
| Area | Before | Now |
|---|---|---|
| Lead detail: Activity timeline (<=200 rows), Assignment & transfer history, Checklist history | all rendered with every page view (3 queries + rows) | **on demand**: collapsed panel, fetched on first open via `/leads/<id>/panel/<name>/` (same 404-if-not-visible rule), loaded once, Retry on failure, no-JS link fallback |
| Contact detail: Assignment history, Activity (<=100) | rendered every view | **on demand**, same mechanism (`/contacts/<id>/panel/<name>/`) |
| Assigned To suggestions | n/a | fetched while typing (debounced, abort, memo) |
| Lists (leads, contacts, segments, imports, audit, attendance, holidays, staff) | server-side paginated | unchanged |
| Dashboard / Reports | plain cards and tables, no charts | nothing to lazy-load |
| `<img>` / charts / below-the-fold media | none exist | n/a — `loading="lazy"` has nothing to apply to |
| Polling | import/job progress already slows when hidden; staff-activity heartbeat is essential | unchanged |
| Background tasks | imports, bulk jobs, Sheet sync already use threads/queue | unchanged; no new infrastructure |
Not suitable / not done: attendance, staff, settings, calling pages (small bounded lists; collapsing them would hide
primary content). `lead_create` still calls n8n synchronously (product decision, see `performance_audit.md`).

## 7. Responsive audit (headless Chromium, real pages, seeded throw-away DB)
Round 1: overflow at 7 widths on 6 core pages. Round 2 (second revision): 238 page x width measurements - login,
register, and as admin: dashboard, All/My Leads, Add/Edit Lead, lead detail, contacts (list/add/detail), segments, staff,
staff attendance, calling, analytics, document analytics, follow-ups, reports, imports, audit log, settings, attendance
review, holidays, staff activity, profile; as staff: dashboard, My/All Leads, lead detail, contacts, attendance,
follow-ups - at 320/375/430/768/820/1024/1366 px. Result: no page-level horizontal overflow and no element extending past
the viewport outside an intentionally scrollable container (a measured result for the seeded data, including 60-80
character assignee names). The
suggestion list opens inside the viewport at 375 px with 44 px touch rows. Existing card-mode tables (`responsive_tables.js`)
pick up the new "Assigned To" / "CRM Owner" headers as card labels automatically. Desktop markup is unchanged except
the new filter box and table column.

## 8. Measured (SQLite, 60 leads, shared sandbox: timings are noisy and NOT representative of production)
Queries per request, original -> now: lead detail 12 -> 9, contact detail 11 -> 9, All Leads 10 -> 10, My Leads 11 -> 11,
Contacts 9 -> 9, All Leads with the assignee filter 10 (param ignored by the old code) -> 11 (one grouped spelling
lookup). No production-scale timing was possible; no percentage claims are made.


## 10. Second revision (this file supersedes the claims above where they differ)

**Assigned To accepts ANY name.** `Lead.assigned_to_name` is a free-text column with no foreign key, no choice list and
no staff lookup. Verified paths, each with tests in `crm/tests_assignee_and_contact_status.py`:
* Add/Edit Lead form: any text up to 200 chars is saved exactly (ends trimmed). Blank and >200 chars are the only
  rejections. Nothing is replaced by the "closest" staff member and no staff/user row is ever created.
* **Bug fixed — inbound n8n API (`/api/leads/create/`):** it answered `400 "Assigned To staff member '...' was not found"`
  for every assignee outside CRM Staff, and it never stored `assigned_to_name` / `reference_by_name`. It now stores the
  typed names (whitespace tidied) and returns 400 only for >200 characters. *(Superseded: the earlier note that the CRM-owner
  FK is set when the text matches a CRM user was withdrawn; ownership is never derived from the free-text name. See
  `docs/n8n_ownership_policy.md`.)*
* Suggestions are read only from `Lead.assigned_to_name` of leads the requester may see (never from Staff/User), so
  external and imported names are suggested after first use. Similar spellings are suggested but never merged:
  "Anchal Jain" and "Anchal Jan" stay separate and each filters only its own leads.
* The Add/Edit Lead form now has the same autocomplete (FIELD mode: pick fills the box, nothing is submitted, any name
  can still be typed; message "No existing match. Keep typing: any name can be used.").
* Filter, KPI cards, CSV + Excel export and the "select all N matching" bulk scope all use the same filter; wildcard
  characters (`%`, `_`, `\`), SQL-looking text, Hindi names, 500-char input and NUL are handled (tests).
* Lead imports: the importer only creates Contacts (there is no lead import path), so there is nothing to change there.
  Contact -> Lead conversion does not pre-fill `assigned_to_name` (the user types it).

**Test infrastructure.** `kota_money/settings_test.py` now ignores `DATABASE_URL` and always uses a throw-away SQLite
file in the temp directory, so a test run cannot reach a production database. Seven tests that failed identically on the
original ZIP were investigated: 6 were stale expectations or clock-dependent (fixed in the tests, not the code:
settings heading text, migration-leaf pin, KM-1 on an empty DB, live-call stale timing at exactly 300 s, lunch GET
depending on the wall clock, staff-attendance export full name) and 1 (concurrent Lead IDs) needed a file-backed SQLite
database with a busy timeout. No test was deleted or weakened to pass.

**Performance / lazy loading.** `QueryCountGuardTests` proves the All Leads / My Leads query count does not grow with
300 rows or 150 distinct assignees and that suggestions run exactly one grouped query. Lazy panels: failure + Retry,
single request on repeated open, fragments are HTML-only, 404 for non-visible records (browser-tested). Still
synchronous by design: `lead_create` posts to n8n inline (15 s timeout, failure never rolls back the lead); moving it to
the existing `LeadSyncEvent` queue changes the user-visible success message, so it is left as a product decision.

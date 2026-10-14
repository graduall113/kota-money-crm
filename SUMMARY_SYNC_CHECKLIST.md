# Summary / KPI synchronisation — page-by-page checklist

Scope: every page of the CRM that shows a summary card, counter, total or badge next to filterable data.
Method: the whole codebase was searched (`stat-card`, `stat-grid`, `aggregate(`, `.count()`, `request.GET`,
`filter_leads`, `filter_contacts`) and every view that reads filters was opened.

**Legend** — ✅ updated in this change · 🟢 already correct (pinned by a test) · 🌐 intentionally global (unchanged) · ➖ no summary on the page

> This is a single-organisation Django app. There are no Organization, Subscription or Billing modules and no
> tenant model; "tenant isolation" here means role isolation — `access.visible_leads/visible_contacts` — and every
> number below is derived from those authorised querysets (or is admin-only).

## Architecture decision: server-side refresh, not AJAX

Every list page in this project is a plain GET form that re-renders the whole page. Adding a second asynchronous
path would mean two code paths that can drift apart — exactly the failure being fixed. So each page recomputes
its records **and** its cards in the same request, from the same filtered queryset. Filters, cards, pagination,
export and bulk actions all see one definition of "the matching records".

## Inventory

| # | Page / module | Summary components | Filters that apply | Status |
|---|---|---|---|---|
| 1 | **My Leads** (`/leads/mine/`) | 6 cards: Assigned to me, Referred by me, Documents Pending, Documents Complete, Meeting Requested, Approved | search, status, assigned/reference, all date ranges, source, work profile, requirement, income, loan range, interest, import batch, follow-up bucket, document status, specific document, **scope tab** | ✅ Cards were computed over *all* visible leads and ignored every filter. Now one `aggregate()` over the same filtered queryset as the table (all pages, not the current page). |
| 2 | **All Leads** (`/leads/all/`) | Header record count only | same filter set as #1 | 🟢 `paginator.count` of the filtered queryset. No cards exist; none were invented. |
| 3 | **Contacts** | Header record count only | search, status, city, assigned, reference, batch, segment, source, work profile, dates, has-lead, follow-up | 🟢 `paginator.count` of the filtered queryset. |
| 4 | **Segment list** | Per-segment member count | none (role-scoped) | ✅ Admin count included soft-deleted contacts that every other screen hides. Now the same rule as `visible_contacts`. Staff count was already correct. |
| 5 | **Segment detail** | "N total contacts · M matching filters" | search, status, assigned | ✅ "N total" was the raw membership count: it included soft-deleted contacts and, for staff, **contacts they may not see** (a count leak). Now `base.count()` — the authorised members. "M matching" was already correct. |
| 6 | **Staff Attendance** (admin) | 10–12 cards (Total Staff, Started, Working, Ended, Present, Half Day, Absent, Sunday/Holiday, Not Started, Auto Ended, Anomalies) | date, date-to, **staff, search** → cards. status, verification, auto-ended, anomaly → table only | ✅ Cards were hard-wired to "today / every staff member". Now: single day → DB aggregates for that date; range → derived from the *same rows* as the table (staff-days). Admins and deactivated staff stay excluded. Card links now keep the date/staff/search. |
| 7 | **Calling Analytics** | 9 totals, per-staff cards, call list | range preset, date from/to, staff | 🟢 Totals, staff cards and list all start from `overview_queryset`; staff users are locked to their own calls. Totals are not limited by the 200-row list cap. |
| 8 | **Document analytics** (admin) | Total / Pending / Not started / Partial / Complete | date range, staff, document status, document | ✅ Cards were taken *before* the document-status filter while both tables applied it. Now one aggregate over the filtered set. The *Document* dropdown only narrows the per-document table (a note on the page says so). |
| 9 | **Performance analytics** (admin) | Per-telecaller table (no cards) | date range | 🟢 Date range reaches every column; staff list is not filtered. |
| 10 | **Dashboard** (admin and staff) | 12 admin / 10 staff cards | none | 🌐 Account-wide (admin) or role-scoped (staff) by design. Pinned by a test: query-string filters do not change them. |
| 11 | **Reports** (admin) | Lead volume by status | none | 🌐 Global status breakdown. |
| 12 | **Follow-ups** | Overdue / Today / Upcoming cards | none | 🌐 Counts come from the same `pending` rule as the lists beneath; lists are capped at 50 rows but counts are not. |
| 13 | **Staff detail** (admin) | Assigned leads / contacts / referred + performance row | none | 🌐 Per-person lifetime figures. |
| 14 | **Import detail / Import list** | Batch counters | none | 🌐 Facts about one batch. |
| 15 | **Attendance events** | "N open" review badge | type, review, outcome, staff, search, dates | 🌐 A review backlog count; it is meant to stay global while you filter the log. |
| 16 | **Holidays** | Today / Next / Year cards, tab counts | tab | 🌐 Calendar facts; tab counts are independent of the tab on purpose. |
| 17 | **Audit log, Staff list, Activity board, Devices, Privileged numbers, Settings, Lead/Contact detail** | none | — | ➖ |

## Related defects fixed on the way (same root cause: two definitions of "the matching set")

* **My Leads scope tab** (`?scope=assigned|referred`) was applied only inside the view. Export and the bulk
  "select all N matching" action rebuild the set from the query string and ignored it, so they could act on *more*
  leads than the table showed. `scope` now lives in `filters.filter_leads(..., user=)`, used by the list, cards,
  export and `jobs.resolve_queryset`. It can only narrow.
* Applying filters on My Leads dropped the scope tab; the form now carries it and "Clear Filters" keeps it.

## Meaning preserved (deliberately not changed)

* My Leads **Documents Pending / Complete** still count `Lead.status` (the pipeline status), not the checklist.
  The Dashboard's "Documents Pending/Complete" use the checklist (`filter_doc_status`). Two definitions behind one
  label already existed; they are unchanged — see "Decision for you" below.
* Attendance cards are a status *breakdown*, so the status filter does not zero them (clicking "Present" would
  otherwise blank every other card). They follow date, staff and search.

## Changed existing tests (intentional)

* `tests_staff_attendance.test_cards_ignore_table_filters` pinned "cards ignore search". Replaced by two tests
  (status doesn't change the cards; staff/search do).
* `tests_performance.SegmentCountTests`: admin count `4 → 3` (soft-deleted contact no longer counted).

## Verification matrix (automated: `python manage.py test crm.tests_summary_sync`)

| Check | My Leads | Segments | Doc analytics | Attendance | Calling | Dashboard |
|---|---|---|---|---|---|---|
| Cards equal an independent count | ✅ (plain ORM filters) | ✅ | ✅ (hand-set fixtures) | ✅ (equal to the table's rows) | ✅ (hand-set fixtures) | — (compared with the unfiltered page) |
| Several filters at once | ✅ | — | ✅ (date+staff+status) | ✅ (range+staff) | ✅ | — |
| Change, then clear → defaults return | ✅ | ✅ | ✅ | ✅ | ✅ (range switch) | — |
| Pagination does not limit totals | ✅ (35 leads, 25/page, pages 1–2) | — | — | ✅ (range, one page) | — (list capped at 200, totals are not) | — |
| Empty result → zeros | ✅ | ✅ | ✅ | ✅ (future date) | — | — |
| Role / isolation | ✅ (two staff + admin) | ✅ (staff / admin) | admin-only | admin-only | ✅ | — |
| Global stays global | — | — | — | — | — | ✅ |

## Manual pass (mobile / tablet / desktop)

The cards are server-rendered with the same values at every width; no viewport-specific code path computes a
number. Spot-check on a narrow window: My Leads → apply *Status = Approved* → the cards and the "These totals cover
N leads…" line change; *Clear Filters* restores them; Staff Attendance → type a name in Search → cards drop to that person.

## Decision for you

My Leads says "Documents Pending" from the **pipeline status**; the Dashboard says it from the **document
checklist**. They can legitimately differ (a lead can be "Documents Pending" with a complete checklist). I kept each
as it was because changing either changes what the number means. If you want one definition everywhere, say which.

## Not yet run

The tests were written and every changed file was compiled and reviewed, but **the suite has not been executed**:
the build sandbox has no Django and no network. Run `python manage.py test crm` once before deploying.


## Addendum (second revision)

* My Leads cards and the All/My Leads record count also follow the new lead **Assigned To** filter
  (`assignee` = `Lead.assigned_to_name`, free text, any name). It is applied inside `filters.filter_leads`, the single
  definition shared by the list, cards, export and bulk "select all". Tests: `LeadAssignedToFilterTests`,
  `FreeTextAssigneeTests` (`crm/tests_assignee_and_contact_status.py`).
* Run the suite with `python manage.py test --settings=kota_money.settings_test`.
* Responsive measurements in this file's earlier "Manual pass" were by inspection; the measured browser results are in
  `docs/final_audit_assignee_status_lazy.md` section 7 / 10.
